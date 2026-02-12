import httpx
import base64
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from zoneinfo import ZoneInfo

from src.config import settings
from src.logger import setup_logger
from src.poweron.schemas import ScheduleResponse
from src.database.engine import async_session
from src.database.models import User, ScheduleCache
from src.poweron.utils import format_schedule, format_date_ua, get_current_status

logger = setup_logger(__name__, settings.LOG_LEVEL)


class PowerService:
    def __init__(self):
        city_id_base64 = base64.b64encode(str(settings.CITY_ID).encode()).decode()

        self.base_url = settings.API_URL
        self.headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
            "Referer": "https://poweron.toe.com.ua/",
            "X-debug-key": city_id_base64,
            "Accept": "application/ld+json",
        }

    @staticmethod
    async def get_schedule_from_cache(date_str: str, group: str = "3.2"):
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
                    cache_time = cache_time.replace(tzinfo=timezone.utc)

                time_diff = (datetime.now(timezone.utc) - cache_time).total_seconds()

                if time_diff < 1800:
                    logger.info(f"Cache HIT for {date_str} (age: {int(time_diff)}s)")
                    return json.loads(cache.times_json), cache_time
                else:
                    logger.info(
                        f"Cache EXPIRED for {date_str} (age: {int(time_diff)}s)"
                    )

            return None, None

    @staticmethod
    async def save_schedule_to_cache(date_str: str, group: str, times_dict: dict):
        async with async_session() as session:
            result = await session.execute(
                select(ScheduleCache).where(
                    ScheduleCache.date_graph == date_str, ScheduleCache.group == group
                )
            )
            cache = result.scalar_one_or_none()

            times_json = json.dumps(times_dict, ensure_ascii=False)

            if cache:
                cache.times_json = times_json
                cache.updated_at = datetime.now(timezone.utc)
                await session.commit()
                logger.info(f"Cache UPDATED for {date_str}")
            else:
                new_cache = ScheduleCache(
                    date_graph=date_str,
                    group=group,
                    times_json=times_json,
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(new_cache)
                await session.commit()
                logger.info(f"Cache SAVED for {date_str}")

    async def get_schedule(self, group: str | None = None):
        target_group = group or settings.DEFAULT_GROUP

        now = datetime.now(timezone.utc)
        after_dt = (now - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        before_dt = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        params = {
            "before": before_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "after": after_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "time": settings.CITY_ID,
        }

        logger.info("🌐 Making API request...")

        async with httpx.AsyncClient(
            headers=self.headers, follow_redirects=True, timeout=30.0
        ) as client:
            response = await client.get(self.base_url, params=params)

            if response.status_code == 200:
                json_data = response.json()
                events = json_data.get("hydra:member", [])

                if events:
                    for event in events:
                        raw_date = event.get("dateGraph")
                        if not raw_date:
                            continue

                        date_graph = raw_date.split("T")[0]
                        data_json = event.get("dataJson", {})

                        group_info = None
                        if isinstance(data_json, dict):
                            group_info = data_json.get(target_group)

                        if date_graph and group_info and target_group:
                            await self.save_schedule_to_cache(
                                date_graph, 
                                target_group,
                                group_info.get("times", {})
                            )

                    return ScheduleResponse(**json_data)

                return None
            else:
                logger.error(f"Error {response.status_code}: {response.text[:200]}")
                return None

    async def get_formatted_schedule(
        self, chat_id: int, date: datetime
    ) -> tuple[str, bool]:
        date_str = date.strftime("%Y-%m-%d")
        date_display = format_date_ua(date)

        async with async_session() as session:
            result = await session.execute(select(User).where(User.chat_id == chat_id))
            user = result.scalar_one_or_none()
            user_group = user.group if user else settings.DEFAULT_GROUP

        cached_times, updated_at = await self.get_schedule_from_cache(
            date_str, user_group
        )

        # if not cached_times:
        #     logger.info(f"Cache miss for {date_str}, fetching from API...")
        #     await self.get_schedule(group=user_group)
        #     cached_times, updated_at = await self.get_schedule_from_cache(
        #         date_str, user_group
        #     )

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
