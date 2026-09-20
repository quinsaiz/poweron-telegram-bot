import base64
import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError

from src.config import settings
from src.database.engine import async_session
from src.database.models import ScheduleCache, User
from src.logger import setup_logger
from src.poweron.schemas import ScheduleResponse
from src.poweron.utils import format_date_ua, format_schedule, get_current_status

logger = setup_logger(__name__, settings.LOG_LEVEL)
TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
VALID_STATUSES = {"0", "1", "10"}


class PowerService:
    def __init__(self) -> None:
        city_id_base64 = base64.b64encode(str(settings.CITY_ID).encode()).decode()

        self.base_url = settings.API_URL
        self.headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
            "Referer": "https://poweron.toe.com.ua/",
            "X-debug-key": city_id_base64,
            "Accept": "application/ld+json",
        }

    @staticmethod
    async def get_schedule_from_cache(
        date_str: str, group: str = "3.2", allow_stale: bool = False
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

                time_diff = (datetime.now(UTC) - cache_time).total_seconds()

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
    async def save_schedule_to_cache(date_str: str, group: str, times_dict: dict[str, str]) -> None:
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(ScheduleCache).where(ScheduleCache.date_graph == date_str)
                )
                cache = result.scalar_one_or_none()
                times_json = json.dumps(times_dict, ensure_ascii=False)

                if cache:
                    cache.group = group
                    cache.times_json = times_json
                    cache.updated_at = datetime.now(UTC)
                else:
                    session.add(
                        ScheduleCache(
                            date_graph=date_str,
                            group=group,
                            times_json=times_json,
                            updated_at=datetime.now(UTC),
                        )
                    )
                await session.commit()
            except DBAPIError, StaleDataError:
                await session.rollback()
                raise

            logger.info(f"Cache {'UPDATED' if cache else 'SAVED'} for {date_str}")

    @staticmethod
    def valid_times(times: dict[str, str]) -> bool:
        return bool(times) and all(
            isinstance(time, str)
            and TIME_PATTERN.fullmatch(time) is not None
            and isinstance(status, str)
            and status in VALID_STATUSES
            for time, status in times.items()
        )

    async def get_schedule(
        self, group: str | None = None, date: datetime | None = None
    ) -> ScheduleResponse | None:
        target_group = group or settings.DEFAULT_GROUP

        now = datetime.now(UTC)
        if date is None:
            after_dt = (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
            before_dt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            requested_midnight = datetime.combine(date.date(), datetime.min.time(), tzinfo=UTC)
            after_dt = requested_midnight - timedelta(hours=12)
            before_dt = requested_midnight + timedelta(days=1, hours=12)

        params: dict[str, str | int] = {
            "before": before_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "after": after_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "time": settings.CITY_ID,
        }

        logger.info("Making API request...")

        async with httpx.AsyncClient(
            headers=self.headers, follow_redirects=True, timeout=30.0
        ) as client:
            response = await client.get(self.base_url, params=params)

            if response.status_code == 200:
                schedule = ScheduleResponse.model_validate(response.json())
                if not schedule.events:
                    return None

                requested_date = date.strftime("%Y-%m-%d") if date is not None else None
                schedules_to_save: dict[str, dict[str, str]] = {}
                for event in schedule.events:
                    date_graph = event.date_graph.split("T")[0]
                    if requested_date is not None and date_graph != requested_date:
                        continue
                    group_info = event.data_json.get(target_group)
                    if group_info is None:
                        continue
                    if not self.valid_times(group_info.times):
                        logger.warning(
                            f"Invalid upstream schedule for {date_graph}, group {target_group}"
                        )
                        return None
                    schedules_to_save.setdefault(date_graph, group_info.times)

                for date_graph, times in schedules_to_save.items():
                    await self.save_schedule_to_cache(date_graph, target_group, times)

                return schedule
            else:
                logger.error(f"Error {response.status_code}: {response.text[:200]}")
                return None

    async def get_formatted_schedule(self, chat_id: int, date: datetime) -> tuple[str, bool]:
        date_str = date.strftime("%Y-%m-%d")
        date_display = format_date_ua(date)

        async with async_session() as session:
            result = await session.execute(select(User).where(User.chat_id == chat_id))
            user = result.scalar_one_or_none()
            user_group = user.group if user else settings.DEFAULT_GROUP

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

        readable_text = format_schedule(cached_times)
        current_status_text = ""
        now = datetime.now()

        if date.date() == now.date():
            status = get_current_status(cached_times)
            if status:
                current_status_text = f"⚡️ **Зараз:** {status}\n"

        kyiv_tz = ZoneInfo("Europe/Kyiv")
        db_time_kyiv = updated_at.astimezone(kyiv_tz)

        caption = (
            f"📅 **Графік на {date_display}**\n"
            f"🏘 Група: **{user_group}**\n"
            f"{current_status_text}"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            f"{readable_text}\n"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            f"💡 _Оновлено о {db_time_kyiv.strftime('%H:%M')}_"
        )

        return caption, True
