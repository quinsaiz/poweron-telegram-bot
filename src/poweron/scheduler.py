"""Durable schedule discovery and at-least-once Telegram delivery.

The per-process locks prevent overlapping loops from deliberately selecting the same
delivery. ``attempt_count`` counts every completed Telegram call, whether it succeeds or
returns a recoverable/terminal error; a fifth recoverable result becomes ``exhausted``.
Exactly-once delivery is impossible: Telegram may accept a message immediately before the
process exits and before SQLite records success. That narrow crash window can produce a
duplicate after restart; all ordinary completed deliveries are unique by
``(event_id, chat_id)`` and are never selected again.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from aiogram import Bot  # noqa: TC002  # runtime scheduler annotation reflection
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError

from src.config import settings
from src.database.engine import async_session
from src.database.models import NotificationDelivery, ProcessedScheduleEvent, User
from src.logger import setup_logger
from src.poweron.service import PowerService, ScheduleFetchResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.poweron.schemas import ScheduleMember
    from src.poweron.service import UsableScheduleEvent

logger = setup_logger(__name__, settings.LOG_LEVEL)

POLL_INTERVAL_SECONDS = 600
MAX_TRANSIENT_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600
MAX_ERROR_LENGTH = 500


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    EXHAUSTED = "exhausted"
    TERMINAL = "terminal"


class EventPersistenceResult(StrEnum):
    CREATED = "created"
    EXISTING_COMPATIBLE = "existing_compatible"
    EXISTING_INCOMPATIBLE = "existing_incompatible"
    FAILED = "failed"


@dataclass(frozen=True)
class DeliveryAttempt:
    id: int
    chat_id: int
    message: str
    attempt_count: int


@dataclass(frozen=True)
class PreparedEvent:
    usable: UsableScheduleEvent
    messages: dict[str, str]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sanitize_error(error: BaseException) -> str:
    if isinstance(error, TelegramRetryAfter):
        return f"TelegramRetryAfter: retry after {error.retry_after}s"

    description = " ".join(str(error).split())
    if settings.BOT_TOKEN:
        description = description.replace(settings.BOT_TOKEN, "[redacted]")
    description = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[redacted]", description)
    result = f"{type(error).__name__}: {description}" if description else type(error).__name__
    return result[:MAX_ERROR_LENGTH]


def _retry_delay(attempt_count: int) -> int:
    return int(min(BASE_BACKOFF_SECONDS * 2 ** max(0, attempt_count - 1), MAX_BACKOFF_SECONDS))


class ScheduleScheduler:
    def __init__(
        self,
        bot: Bot,
        *,
        service: PowerService | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        clock: Callable[[], datetime] = utc_now,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.bot = bot
        self.service = service or PowerService()
        self.session_factory = session_factory or async_session
        self.clock = clock
        self.poll_interval = poll_interval
        self.sleep = sleep
        self._discovery_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()

    async def _load_users(self) -> list[User]:
        async with self.session_factory() as session:
            return list((await session.execute(select(User))).scalars().all())

    async def discover_events(self) -> int:
        async with self._discovery_lock:
            fetch = await self.service.fetch_schedule()
            return await self._persist_usable_events(fetch)

    def _prepare_event(
        self,
        event: ScheduleMember,
        subscribed_groups: set[str],
        relevant_dates: frozenset[date],
        discovered_at: datetime,
    ) -> PreparedEvent | None:
        available_groups = (
            {group for group in event.data_json if isinstance(group, str)}
            if isinstance(event.data_json, dict)
            else set()
        )
        usable = self.service.usable_event(event, available_groups, relevant_dates)
        if usable is None:
            logger.warning("Ignoring unusable schedule event id=%s", event.id)
            return None
        if subscribed_groups and not subscribed_groups.intersection(usable.group_times):
            logger.warning("Ignoring schedule event id=%s without a subscribed group", event.id)
            return None

        messages: dict[str, str] = {}
        for group, times in usable.group_times.items():
            try:
                messages[group] = self.service.render_notification(
                    times, group, usable.event_date, discovered_at
                )
            except (KeyError, TypeError, ValueError) as error:
                logger.warning(
                    "Could not render schedule event id=%s group=%s: %s",
                    event.id,
                    group,
                    _sanitize_error(error),
                )

        required_groups = subscribed_groups or set(usable.group_times)
        if not required_groups.intersection(messages):
            logger.warning("Ignoring unrenderable schedule event id=%s", event.id)
            return None
        return PreparedEvent(usable, messages)

    @staticmethod
    def _snapshot_key(prepared: PreparedEvent) -> tuple[object, ...]:
        usable = prepared.usable
        return (
            usable.date_graph,
            usable.event_date.isoformat(),
            tuple(
                sorted(
                    (group, tuple(sorted(times.items())))
                    for group, times in usable.group_times.items()
                )
            ),
        )

    def _reconcile_events(
        self,
        fetch: ScheduleFetchResult,
        subscribed_groups: set[str],
        discovered_at: datetime,
    ) -> list[PreparedEvent]:
        if fetch.response is None:
            return []

        representations: dict[str, list[PreparedEvent]] = {}
        event_ids: list[str] = []
        for event in fetch.response.events:
            if event.id is None:
                logger.warning("Ignoring schedule event without a valid id")
                continue
            prepared = self._prepare_event(
                event, subscribed_groups, fetch.relevant_dates, discovered_at
            )
            if prepared is None:
                continue
            event_id = prepared.usable.event_id
            if event_id not in representations:
                event_ids.append(event_id)
                representations[event_id] = []
            representations[event_id].append(prepared)

        accepted: list[PreparedEvent] = []
        for event_id in event_ids:
            duplicates = representations[event_id]
            first = duplicates[0]
            if any(
                self._snapshot_key(candidate) != self._snapshot_key(first)
                for candidate in duplicates[1:]
            ):
                logger.warning("Ignoring conflicting schedule members for event id=%s", event_id)
                continue
            accepted.append(first)
        return accepted

    async def _persist_event(
        self, prepared: PreparedEvent, users: list[User], discovered_at: datetime
    ) -> EventPersistenceResult:
        usable = prepared.usable
        deliveries = [
            NotificationDelivery(
                event_id=usable.event_id,
                chat_id=user.chat_id,
                recipient_group=user.group,
                event_date=usable.event_date.isoformat(),
                message=prepared.messages[user.group],
                status=DeliveryStatus.PENDING.value,
                attempt_count=0,
                next_attempt_at=None,
                created_at=discovered_at,
                updated_at=discovered_at,
                sent_at=None,
                last_error=None,
            )
            for user in users
            if user.group in prepared.messages
        ]

        try:
            async with self.session_factory() as session:
                existing = await session.get(ProcessedScheduleEvent, usable.event_id)
                if existing is not None:
                    if existing.date_graph != usable.date_graph:
                        logger.warning(
                            "Existing schedule event id=%s has incompatible dateGraph; "
                            "cache unchanged",
                            usable.event_id,
                        )
                        return EventPersistenceResult.EXISTING_INCOMPATIBLE
                    return EventPersistenceResult.EXISTING_COMPATIBLE
                session.add(
                    ProcessedScheduleEvent(
                        event_id=usable.event_id,
                        date_graph=usable.date_graph,
                        created_at=discovered_at,
                    )
                )
                # No ORM relationship is needed, so explicitly flush the FK parent
                # before its children while retaining one atomic transaction.
                await session.flush()
                session.add_all(deliveries)
                await session.commit()
            return EventPersistenceResult.CREATED
        except Exception:
            logger.exception("Could not persist schedule event id=%s", usable.event_id)
            return EventPersistenceResult.FAILED

    async def _refresh_cache(self, accepted: list[PreparedEvent], discovered_at: datetime) -> None:
        candidates: dict[tuple[str, str], list[dict[str, str]]] = {}
        for prepared in accepted:
            date_graph = prepared.usable.event_date.isoformat()
            for group, times in prepared.usable.group_times.items():
                candidates.setdefault((date_graph, group), []).append(times)

        for (date_graph, group), schedules in candidates.items():
            first = schedules[0]
            if any(schedule != first for schedule in schedules[1:]):
                logger.warning(
                    "Conflicting accepted schedules for %s, group %s; cache unchanged",
                    date_graph,
                    group,
                )
                continue
            try:
                async with self.session_factory() as session, session.begin():
                    await self.service.upsert_schedule_cache(
                        session, date_graph, group, first, discovered_at
                    )
            except DBAPIError, StaleDataError:
                # Events and deliveries were committed first; one cache-key failure
                # cannot roll them back or prevent independent keys from refreshing.
                logger.exception(
                    "Could not refresh schedule cache for %s, group %s",
                    date_graph,
                    group,
                )
            except Exception:
                logger.exception(
                    "Unexpected cache refresh failure for %s, group %s",
                    date_graph,
                    group,
                )

    async def _persist_usable_events(self, fetch: ScheduleFetchResult) -> int:
        if fetch.response is None or not fetch.response.events:
            logger.info("The schedule is empty or unavailable.")
            return 0

        users = await self._load_users()
        subscribed_groups = {user.group for user in users}
        discovered_at = _as_utc(self.clock())
        accepted = self._reconcile_events(fetch, subscribed_groups, discovered_at)
        persisted = 0
        cacheable: list[PreparedEvent] = []
        for prepared in accepted:
            result = await self._persist_event(prepared, users, discovered_at)
            match result:
                case EventPersistenceResult.CREATED:
                    persisted += 1
                    cacheable.append(prepared)
                case EventPersistenceResult.EXISTING_COMPATIBLE:
                    cacheable.append(prepared)
                case EventPersistenceResult.EXISTING_INCOMPATIBLE | EventPersistenceResult.FAILED:
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
        await self._refresh_cache(cacheable, discovered_at)
        return persisted

    async def _due_delivery_ids(self, now: datetime) -> list[int]:
        now = _as_utc(now)
        async with self.session_factory() as session:
            result = await session.execute(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.status == DeliveryStatus.PENDING.value,
                    or_(
                        NotificationDelivery.next_attempt_at.is_(None),
                        NotificationDelivery.next_attempt_at <= now,
                    ),
                )
                .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
            )
            return list(result.scalars().all())

    async def _load_due_delivery(self, delivery_id: int, now: datetime) -> DeliveryAttempt | None:
        now = _as_utc(now)
        async with self.session_factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            if (
                delivery is None
                or delivery.status != DeliveryStatus.PENDING.value
                or (
                    delivery.next_attempt_at is not None and _as_utc(delivery.next_attempt_at) > now
                )
            ):
                return None
            return DeliveryAttempt(
                id=delivery.id,
                chat_id=delivery.chat_id,
                message=delivery.message,
                attempt_count=delivery.attempt_count,
            )

    async def _record_success(self, attempt: DeliveryAttempt, completed_at: datetime) -> None:
        completed_at = _as_utc(completed_at)
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.id == attempt.id,
                    NotificationDelivery.status == DeliveryStatus.PENDING.value,
                )
                .values(
                    status=DeliveryStatus.SENT.value,
                    attempt_count=attempt.attempt_count + 1,
                    updated_at=completed_at,
                    sent_at=completed_at,
                    last_error=None,
                    next_attempt_at=None,
                )
            )

    async def _record_transient_failure(
        self,
        attempt: DeliveryAttempt,
        error: BaseException,
        completed_at: datetime,
        *,
        retry_after: float | None = None,
    ) -> None:
        completed_at = _as_utc(completed_at)
        new_attempt_count = attempt.attempt_count + 1
        exhausted = new_attempt_count >= MAX_TRANSIENT_ATTEMPTS
        delay = (
            max(0.0, retry_after)
            if retry_after is not None
            else float(_retry_delay(new_attempt_count))
        )
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.id == attempt.id,
                    NotificationDelivery.status == DeliveryStatus.PENDING.value,
                )
                .values(
                    status=(
                        DeliveryStatus.EXHAUSTED.value
                        if exhausted
                        else DeliveryStatus.PENDING.value
                    ),
                    attempt_count=new_attempt_count,
                    next_attempt_at=(
                        None if exhausted else completed_at + timedelta(seconds=delay)
                    ),
                    updated_at=completed_at,
                    last_error=_sanitize_error(error),
                )
            )

    async def _record_terminal_failure(
        self, attempt: DeliveryAttempt, error: BaseException, completed_at: datetime
    ) -> None:
        completed_at = _as_utc(completed_at)
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.id == attempt.id,
                    NotificationDelivery.status == DeliveryStatus.PENDING.value,
                )
                .values(
                    status=DeliveryStatus.TERMINAL.value,
                    attempt_count=attempt.attempt_count + 1,
                    updated_at=completed_at,
                    last_error=_sanitize_error(error),
                    next_attempt_at=None,
                )
            )
            await session.execute(delete(User).where(User.chat_id == attempt.chat_id))

    async def _deliver_one(self, delivery_id: int) -> bool:
        attempt = await self._load_due_delivery(delivery_id, self.clock())
        if attempt is None:
            return False

        try:
            await self.bot.send_message(attempt.chat_id, attempt.message, parse_mode="Markdown")
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as error:
            await self._record_transient_failure(
                attempt, error, self.clock(), retry_after=float(error.retry_after)
            )
        except (TelegramForbiddenError, TelegramNotFound) as error:
            await self._record_terminal_failure(attempt, error, self.clock())
        except (TelegramAPIError, ClientDecodeError) as error:
            await self._record_transient_failure(attempt, error, self.clock())
        except Exception:
            logger.exception(
                "Unexpected local error while sending delivery id=%s; delivery remains pending",
                attempt.id,
            )
        else:
            await self._record_success(attempt, self.clock())
            return True
        return False

    async def process_due_deliveries(self) -> int:
        """Attempt each currently due row once without a DB transaction during Telegram I/O."""

        async with self._delivery_lock:
            delivery_ids = await self._due_delivery_ids(self.clock())
            sent = 0
            for delivery_id in delivery_ids:
                try:
                    sent += await self._deliver_one(delivery_id)
                except asyncio.CancelledError:
                    raise
                except DBAPIError:
                    logger.exception("Could not update delivery id=%s", delivery_id)
                except Exception:
                    logger.exception("Unexpected delivery processing error id=%s", delivery_id)
            return sent

    async def _run_phase(self, name: str, phase: Callable[[], Awaitable[object]]) -> None:
        try:
            await phase()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s failed; scheduler will retry", name)

    async def run(self) -> None:
        await self._run_phase("Startup delivery scan", self.process_due_deliveries)
        while True:
            logger.info("Checking for schedule updates...")
            await self._run_phase("Schedule discovery", self.discover_events)
            await self._run_phase("Post-discovery delivery scan", self.process_due_deliveries)
            await self.sleep(self.poll_interval)


async def check_updates_loop(bot: Bot) -> None:
    await ScheduleScheduler(bot).run()
