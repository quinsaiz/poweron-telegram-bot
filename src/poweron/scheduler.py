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
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

import httpx
from aiogram import Bot  # noqa: TC002  # runtime scheduler annotation reflection
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from pydantic import ValidationError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm.exc import StaleDataError

from src.config import settings
from src.database.engine import async_session
from src.database.errors import TRANSIENT_DATABASE_EXCEPTIONS
from src.database.models import NotificationDelivery, ProcessedScheduleEvent, User
from src.database.source_state import source_authority_transaction
from src.domain_time import utc_now
from src.logger import setup_logger
from src.poweron.groups import SourceIdentityError
from src.poweron.service import PowerService, ScheduleFetchResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import date

    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.poweron.schemas import ScheduleMember
    from src.poweron.service import UsableScheduleEvent

logger = setup_logger(__name__, settings.LOG_LEVEL)

POLL_INTERVAL_SECONDS = 600
MAX_TRANSIENT_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600
MAX_ERROR_LENGTH = 500
BOT_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:bot)?[0-9]+:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)
EXPECTED_PERSISTENCE_EXCEPTIONS: tuple[type[SQLAlchemyError], ...] = (
    *TRANSIENT_DATABASE_EXCEPTIONS,
    StaleDataError,
)


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
    STALE_AUTHORITY = "stale_authority"


class StartupGroupRefreshState(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    TRANSIENT_FAILURE = "transient_failure"


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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sanitize_error(error: BaseException) -> str:
    if isinstance(error, TelegramRetryAfter):
        return f"TelegramRetryAfter: retry after {error.retry_after}s"

    description = " ".join(str(error).split())
    description = BOT_TOKEN_PATTERN.sub("[redacted-bot-token]", description)
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

    def _clock_now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Scheduler clock must return an aware datetime")
        return value.astimezone(UTC)

    async def _load_users(self) -> list[User]:
        async with self.session_factory() as session:
            return list((await session.execute(select(User))).scalars().all())

    async def discover_events(self) -> int:
        async with self._discovery_lock:
            city_id = settings.POWERON_CITY_ID
            group = await self.service.group_resolver.ensure_group()
            if group is None:
                logger.warning("Skipping schedule discovery: poweron group is unavailable")
                return 0
            try:
                fetch = await self.service.fetch_schedule(group)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, json.JSONDecodeError, ValidationError) as error:
                logger.warning(
                    "poweron schedule discovery is unavailable: %s",
                    type(error).__name__,
                )
                return 0
            return await self._persist_usable_events(fetch, city_id, group)

    def _prepare_event(
        self,
        event: ScheduleMember,
        group: str,
        relevant_dates: frozenset[date],
        discovered_at: datetime,
    ) -> PreparedEvent | None:
        usable = self.service.usable_event(event, {group}, relevant_dates)
        if usable is None:
            logger.warning("Ignoring unusable schedule event id=%s", event.id)
            return None

        messages: dict[str, str] = {}
        for event_group, times in usable.group_times.items():
            try:
                messages[event_group] = self.service.render_notification(
                    times,
                    event_group,
                    usable.event_date,
                    discovered_at,
                    now=discovered_at,
                )
            except (KeyError, TypeError, ValueError) as error:
                logger.warning(
                    "Could not render schedule event id=%s group=%s: %s",
                    event.id,
                    event_group,
                    _sanitize_error(error),
                )

        if group not in messages:
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
        group: str,
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
            prepared = self._prepare_event(event, group, fetch.relevant_dates, discovered_at)
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
        self,
        prepared: PreparedEvent,
        users: list[User],
        discovered_at: datetime,
        city_id: int,
        group: str,
    ) -> EventPersistenceResult:
        usable = prepared.usable
        recipient_group, message = next(iter(prepared.messages.items()))
        deliveries = [
            NotificationDelivery(
                event_id=usable.event_id,
                chat_id=user.chat_id,
                recipient_group=recipient_group,
                event_date=usable.event_date.isoformat(),
                message=message,
                status=DeliveryStatus.PENDING.value,
                attempt_count=0,
                next_attempt_at=None,
                created_at=discovered_at,
                updated_at=discovered_at,
                sent_at=None,
                last_error=None,
            )
            for user in users
        ]

        try:
            async with source_authority_transaction(
                self.session_factory,
                city_id=city_id,
                group=group,
            ) as session:
                if session is None:
                    logger.info(
                        "Skipping stale schedule event id=%s for group=%s",
                        usable.event_id,
                        group,
                    )
                    return EventPersistenceResult.STALE_AUTHORITY
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
            return EventPersistenceResult.CREATED
        except EXPECTED_PERSISTENCE_EXCEPTIONS:
            logger.exception("Could not persist schedule event id=%s", usable.event_id)
            return EventPersistenceResult.FAILED

    async def _refresh_cache(
        self,
        accepted: list[PreparedEvent],
        discovered_at: datetime,
        city_id: int,
        expected_group: str,
    ) -> None:
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
                async with source_authority_transaction(
                    self.session_factory,
                    city_id=city_id,
                    group=expected_group,
                ) as session:
                    if session is None:
                        logger.info(
                            "Skipping stale schedule cache for %s, group=%s",
                            date_graph,
                            group,
                        )
                        return
                    await self.service.upsert_schedule_cache(
                        session, date_graph, group, first, discovered_at
                    )
            except EXPECTED_PERSISTENCE_EXCEPTIONS:
                # Events and deliveries were committed first; one cache-key failure
                # cannot roll them back or prevent independent keys from refreshing.
                logger.exception(
                    "Could not refresh schedule cache for %s, group %s",
                    date_graph,
                    group,
                )

    async def _persist_usable_events(
        self, fetch: ScheduleFetchResult, city_id: int, group: str
    ) -> int:
        if fetch.response is None or not fetch.response.events:
            logger.info("The schedule is empty or unavailable.")
            return 0

        users = await self._load_users()
        discovered_at = self._clock_now()
        accepted = self._reconcile_events(fetch, group, discovered_at)
        persisted = 0
        cacheable: list[PreparedEvent] = []
        for prepared in accepted:
            result = await self._persist_event(
                prepared,
                users,
                discovered_at,
                city_id,
                group,
            )
            match result:
                case EventPersistenceResult.CREATED:
                    persisted += 1
                    cacheable.append(prepared)
                case EventPersistenceResult.EXISTING_COMPATIBLE:
                    cacheable.append(prepared)
                case EventPersistenceResult.EXISTING_INCOMPATIBLE | EventPersistenceResult.FAILED:
                    pass
                case EventPersistenceResult.STALE_AUTHORITY:
                    return persisted
                case _ as unreachable:
                    assert_never(unreachable)
        await self._refresh_cache(cacheable, discovered_at, city_id, group)
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
        attempt = await self._load_due_delivery(delivery_id, self._clock_now())
        if attempt is None:
            return False

        try:
            await self.bot.send_message(attempt.chat_id, attempt.message, parse_mode="Markdown")
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as error:
            await self._record_transient_failure(
                attempt, error, self._clock_now(), retry_after=float(error.retry_after)
            )
        except (TelegramForbiddenError, TelegramNotFound) as error:
            await self._record_terminal_failure(attempt, error, self._clock_now())
        except (TelegramAPIError, ClientDecodeError) as error:
            await self._record_transient_failure(attempt, error, self._clock_now())
        except Exception:
            logger.exception(
                "Unexpected local error while sending delivery id=%s; delivery remains pending",
                attempt.id,
            )
        else:
            await self._record_success(attempt, self._clock_now())
            return True
        return False

    async def process_due_deliveries(self) -> int:
        """Attempt each currently due row once without a DB transaction during Telegram I/O."""

        async with self._delivery_lock:
            delivery_ids = await self._due_delivery_ids(self._clock_now())
            sent = 0
            for delivery_id in delivery_ids:
                try:
                    sent += await self._deliver_one(delivery_id)
                except asyncio.CancelledError:
                    raise
                except TRANSIENT_DATABASE_EXCEPTIONS:
                    logger.exception("Could not update delivery id=%s", delivery_id)
            return sent

    async def _run_phase(self, name: str, phase: Callable[[], Awaitable[object]]) -> bool:
        try:
            await phase()
        except asyncio.CancelledError:
            raise
        except SourceIdentityError:
            raise
        except TRANSIENT_DATABASE_EXCEPTIONS:
            logger.exception("%s failed; scheduler will retry", name)
            return False
        return True

    async def _confirm_source_identity(self) -> bool:
        try:
            await self.service.group_resolver.ensure_source_identity()
        except asyncio.CancelledError:
            raise
        except SourceIdentityError:
            raise
        except TRANSIENT_DATABASE_EXCEPTIONS:
            logger.exception("poweron source identity check failed; scheduler will retry")
            return False
        return True

    async def _complete_startup_phases(
        self,
        refresh_state: StartupGroupRefreshState,
        delivery_scanned: bool,
    ) -> tuple[bool, bool]:
        if refresh_state is StartupGroupRefreshState.TRANSIENT_FAILURE:
            if not delivery_scanned:
                await self._run_phase("Startup delivery scan", self.process_due_deliveries)
                delivery_scanned = True
            await self.sleep(self.poll_interval)
            return False, delivery_scanned

        if refresh_state is StartupGroupRefreshState.NOT_ATTEMPTED:
            refresh_succeeded = await self._run_phase(
                "Startup group refresh", self.service.group_resolver.ensure_group
            )
            if not delivery_scanned:
                await self._run_phase("Startup delivery scan", self.process_due_deliveries)
                delivery_scanned = True
            if not refresh_succeeded:
                await self.sleep(self.poll_interval)
                return False, delivery_scanned
        elif refresh_state is not StartupGroupRefreshState.SUCCEEDED:
            assert_never(refresh_state)

        if not delivery_scanned:
            await self._run_phase("Startup delivery scan", self.process_due_deliveries)
            delivery_scanned = True
        return True, delivery_scanned

    async def run(
        self,
        startup_group_refresh_state: StartupGroupRefreshState = (
            StartupGroupRefreshState.NOT_ATTEMPTED
        ),
    ) -> None:
        identity_confirmed = False
        startup_phases_complete = False
        startup_delivery_scanned = False
        while True:
            if not identity_confirmed:
                identity_confirmed = await self._confirm_source_identity()
                if not identity_confirmed:
                    await self.sleep(self.poll_interval)
                    continue
            if not startup_phases_complete:
                (
                    startup_phases_complete,
                    startup_delivery_scanned,
                ) = await self._complete_startup_phases(
                    startup_group_refresh_state,
                    startup_delivery_scanned,
                )
                startup_group_refresh_state = StartupGroupRefreshState.NOT_ATTEMPTED
                if not startup_phases_complete:
                    continue
            logger.info("Checking for schedule updates...")
            await self._run_phase("Schedule discovery", self.discover_events)
            await self._run_phase("Post-discovery delivery scan", self.process_due_deliveries)
            await self.sleep(self.poll_interval)


async def check_updates_loop(
    bot: Bot,
    *,
    startup_group_refresh_state: StartupGroupRefreshState = (
        StartupGroupRefreshState.NOT_ATTEMPTED
    ),
) -> None:
    await ScheduleScheduler(bot).run(startup_group_refresh_state)
