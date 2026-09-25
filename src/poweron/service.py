from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as calendar_date
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError

from src.config import settings
from src.database.engine import async_session
from src.database.models import ScheduleCache
from src.database.source_state import source_authority_transaction
from src.domain_time import KYIV_TZ, as_kyiv, kyiv_now, utc_now
from src.logger import setup_logger
from src.poweron.groups import PowerOnGroupResolver, group_resolver
from src.poweron.schemas import GroupData, ScheduleMember, ScheduleResponse, parse_date_graph
from src.poweron.utils import format_date_ua, format_schedule, get_current_status

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = setup_logger(__name__, settings.LOG_LEVEL)
TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
VALID_STATUSES = {"0", "1", "10"}
GROUP_UNAVAILABLE_MESSAGE = "⚠️ **Інформація про групу тимчасово недоступна. Спробуйте пізніше.**"


@dataclass(frozen=True)
class ScheduleFetchResult:
    """A validated raw response and the local dates its request may satisfy."""

    response: ScheduleResponse | None
    relevant_dates: frozenset[calendar_date]


@dataclass(frozen=True)
class DiscoveryFetchWindow:
    after: datetime
    before: datetime
    relevant_dates: frozenset[calendar_date]


@dataclass(frozen=True)
class UsableScheduleEvent:
    event_id: str
    date_graph: str
    event_date: calendar_date
    group_times: dict[str, dict[str, str]]


@dataclass(frozen=True)
class CacheRefreshResult:
    fetch: ScheduleFetchResult
    usable_events: tuple[UsableScheduleEvent, ...]
    cached_dates: tuple[str, ...]

    @property
    def has_usable_event(self) -> bool:
        return bool(self.usable_events)


class PowerService:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        resolver: PowerOnGroupResolver | None = None,
    ) -> None:
        self.clock = clock or utc_now
        self.group_resolver = resolver or group_resolver
        city_id_base64 = base64.b64encode(str(settings.POWERON_CITY_ID).encode()).decode()

        self.schedule_url = f"{settings.POWERON_API_URL}/a_gpv_g"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
            "Referer": "https://poweron.toe.com.ua/",
            "X-debug-key": city_id_base64,
            "Accept": "application/ld+json",
        }

    @staticmethod
    def discovery_fetch_window(now: datetime) -> DiscoveryFetchWindow:
        now_kyiv = as_kyiv(now, source="Discovery clock")
        local_today = now_kyiv.date()
        relevant_dates = frozenset((local_today, local_today + timedelta(days=1)))

        # The live endpoint currently returned events equal to both after and before.
        # Surrounding Kyiv midnights and exact relevant-date filtering keep correctness
        # independent of undocumented boundary behavior.
        after_local = datetime.combine(local_today - timedelta(days=1), time.min, tzinfo=KYIV_TZ)
        before_local = datetime.combine(local_today + timedelta(days=2), time.min, tzinfo=KYIV_TZ)
        return DiscoveryFetchWindow(
            after=after_local.astimezone(UTC),
            before=before_local.astimezone(UTC),
            relevant_dates=relevant_dates,
        )

    @staticmethod
    async def get_schedule_from_cache(
        date_str: str, group: str, allow_stale: bool = False
    ) -> tuple[dict[str, str] | None, datetime | None]:
        async with async_session() as session:
            result = await session.execute(
                select(ScheduleCache).where(
                    ScheduleCache.date_graph == date_str, ScheduleCache.group == group
                )
            )
            cache = result.scalar_one_or_none()

            if cache:
                cache_time = cache.updated_at
                if cache_time.tzinfo is None:
                    cache_time = cache_time.replace(tzinfo=UTC)

                time_diff = (utc_now() - cache_time).total_seconds()

                if time_diff >= 1800 and not allow_stale:
                    logger.info(f"Cache EXPIRED for {date_str} (age: {int(time_diff)}s)")
                    return None, None

                try:
                    times = json.loads(cache.times_json)
                except TypeError, ValueError:
                    logger.warning(f"Invalid cached schedule for {date_str}, group {group}")
                    return None, None

                if not isinstance(times, dict) or not times:
                    return None, None

                logger.info(f"Cache HIT for {date_str} (age: {int(time_diff)}s)")
                return times, cache_time

            return None, None

    @staticmethod
    async def upsert_schedule_cache(
        session: AsyncSession,
        date_str: str,
        group: str,
        times_dict: dict[str, str],
        updated_at: datetime,
    ) -> None:
        times_json = json.dumps(times_dict, ensure_ascii=False)
        statement = insert(ScheduleCache).values(
            date_graph=date_str,
            group=group,
            times_json=times_json,
            updated_at=updated_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ScheduleCache.date_graph, ScheduleCache.group],
            set_={
                "times_json": statement.excluded.times_json,
                "updated_at": statement.excluded.updated_at,
            },
        )
        await session.execute(statement)

    @classmethod
    async def save_schedule_to_cache(
        cls, date_str: str, group: str, times_dict: dict[str, str]
    ) -> None:
        async with async_session() as session:
            try:
                await cls.upsert_schedule_cache(
                    session,
                    date_str,
                    group,
                    times_dict,
                    utc_now(),
                )
                await session.commit()
            except DBAPIError, StaleDataError:
                await session.rollback()
                raise

            logger.info("Cache saved for %s, group %s", date_str, group)

    @classmethod
    async def save_authoritative_schedule_to_cache(
        cls,
        date_str: str,
        group: str,
        times_dict: dict[str, str],
        *,
        city_id: int,
    ) -> bool:
        async with source_authority_transaction(
            async_session,
            city_id=city_id,
            group=group,
        ) as session:
            if session is None:
                logger.info("Skipping stale schedule cache for %s, group=%s", date_str, group)
                return False
            await cls.upsert_schedule_cache(session, date_str, group, times_dict, utc_now())

        logger.info("Cache saved for %s, group %s", date_str, group)
        return True

    @staticmethod
    def valid_times(times: dict[str, str]) -> bool:
        return bool(times) and all(
            isinstance(time, str)
            and TIME_PATTERN.fullmatch(time) is not None
            and isinstance(status, str)
            and status in VALID_STATUSES
            for time, status in times.items()
        )

    @staticmethod
    def parse_event_date(date_graph: str | None) -> calendar_date | None:
        parsed = parse_date_graph(date_graph)
        # PowerOn identifies a schedule by the calendar date written on the wire.
        # The offset is still mandatory and validated, but converting the instant can
        # incorrectly move historical payloads to an adjacent Kyiv calendar day.
        return parsed.date() if parsed is not None else None

    @classmethod
    def usable_event(
        cls,
        event: ScheduleMember,
        groups: set[str],
        relevant_dates: frozenset[calendar_date],
    ) -> UsableScheduleEvent | None:
        event_date = cls.parse_event_date(event.date_graph)
        if (
            event.id is None
            or event.date_graph is None
            or not isinstance(event.data_json, dict)
            or event_date is None
            or event_date not in relevant_dates
        ):
            return None

        group_times: dict[str, dict[str, str]] = {}
        for group in groups:
            raw_group = event.data_json.get(group)
            if raw_group is None:
                continue
            try:
                group_info = GroupData.model_validate(raw_group)
            except ValidationError:
                continue
            if cls.valid_times(group_info.times):
                group_times[group] = dict(group_info.times)
        if not group_times:
            return None
        return UsableScheduleEvent(
            event_id=str(event.id),
            date_graph=event.date_graph,
            event_date=event_date,
            group_times=group_times,
        )

    @staticmethod
    def render_schedule_caption(
        times: dict[str, str],
        group: str,
        event_date: calendar_date,
        updated_at: datetime,
        *,
        now: datetime | None = None,
    ) -> str:
        now_kyiv = kyiv_now() if now is None else as_kyiv(now, source="Schedule rendering clock")
        target_datetime = datetime.combine(event_date, datetime.min.time())
        date_display = format_date_ua(target_datetime)
        readable_text = format_schedule(times)
        current_status_text = ""

        if event_date == now_kyiv.date():
            status = get_current_status(times, now=now_kyiv)
            if status:
                current_status_text = f"⚡️ **Зараз:** {status}\n"

        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        db_time_kyiv = updated_at.astimezone(KYIV_TZ)
        return (
            f"📅 **Графік на {date_display}**\n"
            f"🏘 Група: **{group}**\n"
            f"{current_status_text}"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            f"{readable_text}\n"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            f"💡 _Оновлено о {db_time_kyiv.strftime('%H:%M')}_"
        )

    @classmethod
    def render_notification(
        cls,
        times: dict[str, str],
        group: str,
        event_date: calendar_date,
        discovered_at: datetime,
        *,
        now: datetime | None = None,
    ) -> str:
        caption = cls.render_schedule_caption(times, group, event_date, discovered_at, now=now)
        return f"🔔 **ОПУБЛІКОВАНО ОНОВЛЕННЯ!**\n\n{caption}"

    async def fetch_schedule(self, group: str, date: datetime | None = None) -> ScheduleFetchResult:
        if date is None:
            window = self.discovery_fetch_window(self.clock())
            after_dt = window.after
            before_dt = window.before
            relevant_dates = window.relevant_dates
        else:
            requested_midnight = datetime.combine(date.date(), datetime.min.time(), tzinfo=UTC)
            after_dt = requested_midnight - timedelta(hours=12)
            before_dt = requested_midnight + timedelta(days=1, hours=12)
            relevant_dates = frozenset((date.date(),))

        params: list[tuple[str, str | int | float | bool | None]] = [
            ("before", before_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")),
            ("after", after_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")),
            ("group[]", group),
            ("time", settings.POWERON_CITY_ID),
        ]

        logger.info("Making API request...")
        async with httpx.AsyncClient(
            headers=self.headers, follow_redirects=True, timeout=30.0
        ) as client:
            response = await client.get(self.schedule_url, params=params)

        if response.status_code != 200:
            logger.error("PowerOn schedule request failed with status %s", response.status_code)
            return ScheduleFetchResult(None, relevant_dates)

        schedule = ScheduleResponse.model_validate(response.json())
        return ScheduleFetchResult(schedule, relevant_dates)

    async def refresh_schedule(
        self, group: str, date: datetime | None = None
    ) -> CacheRefreshResult:
        city_id = settings.POWERON_CITY_ID
        fetch = await self.fetch_schedule(group, date)
        if fetch.response is None or not fetch.response.events:
            return CacheRefreshResult(fetch, (), ())

        usable_events = tuple(
            usable
            for event in fetch.response.events
            if (usable := self.usable_event(event, {group}, fetch.relevant_dates)) is not None
        )
        candidates: dict[str, list[dict[str, str]]] = {}
        for usable in usable_events:
            candidates.setdefault(usable.event_date.isoformat(), []).append(
                usable.group_times[group]
            )

        cached_dates: list[str] = []
        for date_graph, schedules in candidates.items():
            first_schedule = schedules[0]
            if any(schedule != first_schedule for schedule in schedules[1:]):
                logger.warning(
                    "Ambiguous upstream schedules for %s, group %s; cache unchanged",
                    date_graph,
                    group,
                )
                continue
            saved = await self.save_authoritative_schedule_to_cache(
                date_graph,
                group,
                first_schedule,
                city_id=city_id,
            )
            if not saved:
                break
            cached_dates.append(date_graph)

        return CacheRefreshResult(fetch, usable_events, tuple(cached_dates))

    async def get_schedule(
        self, group: str, date: datetime | None = None
    ) -> ScheduleResponse | None:
        refresh = await self.refresh_schedule(group, date)
        if not refresh.has_usable_event:
            return None
        return refresh.fetch.response

    async def get_formatted_schedule(self, chat_id: int, date: datetime) -> tuple[str, bool]:
        del chat_id
        date_str = date.strftime("%Y-%m-%d")
        date_display = format_date_ua(date)
        user_group = await self.group_resolver.ensure_group()
        if user_group is None:
            return GROUP_UNAVAILABLE_MESSAGE, False

        cached_times, updated_at = await self.get_schedule_from_cache(date_str, user_group)

        if cached_times is None:
            try:
                await self.get_schedule(group=user_group, date=date)
            except (
                httpx.HTTPError,
                json.JSONDecodeError,
                ValidationError,
                DBAPIError,
                StaleDataError,
            ) as exc:
                logger.warning(f"Could not refresh schedule for {date_str}: {exc}")

            cached_times, updated_at = await self.get_schedule_from_cache(date_str, user_group)
            if cached_times is None:
                cached_times, updated_at = await self.get_schedule_from_cache(
                    date_str, user_group, allow_stale=True
                )

        if cached_times is None or updated_at is None:
            return f"❌ **Графіка на {date_display} ще немає**", False

        return (
            self.render_schedule_caption(
                cached_times,
                user_group,
                date.date(),
                updated_at,
                now=self.clock(),
            ),
            True,
        )
