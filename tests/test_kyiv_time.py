from __future__ import annotations

import os
import time
import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from aiogram import types
from src.domain_time import KYIV_TZ, as_kyiv
from src.poweron.utils import get_current_status

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

with patch.dict(
    os.environ,
    {
        "BOT_TOKEN": "123:test-only-token",
        "POWERON_CITY_ID": "21005",
        "POWERON_API_URL": "https://api-poweron.toe.com.ua/api",
    },
):
    from src.poweron.service import PowerService
    from src.telegram import handlers


class KyivTimeTests(unittest.TestCase):
    def test_winter_and_summer_use_kyiv_utc_offsets(self) -> None:
        winter = as_kyiv(datetime(2026, 1, 15, 10, 0, tzinfo=UTC))
        summer = as_kyiv(datetime(2026, 7, 15, 10, 0, tzinfo=UTC))

        self.assertEqual(winter.utcoffset(), timedelta(hours=2))
        self.assertEqual(winter.hour, 12)
        self.assertEqual(summer.utcoffset(), timedelta(hours=3))
        self.assertEqual(summer.hour, 13)

    def test_spring_dst_boundary_uses_zoneinfo_transition(self) -> None:
        before = as_kyiv(datetime(2026, 3, 29, 0, 30, tzinfo=UTC))
        after = as_kyiv(datetime(2026, 3, 29, 1, 30, tzinfo=UTC))

        self.assertEqual(before.utcoffset(), timedelta(hours=2))
        self.assertEqual((before.hour, before.minute), (2, 30))
        self.assertEqual(after.utcoffset(), timedelta(hours=3))
        self.assertEqual((after.hour, after.minute), (4, 30))

    def test_current_status_uses_kyiv_wall_clock(self) -> None:
        times = {"00:00": "0", "12:00": "1", "18:00": "10"}
        now = datetime(2026, 7, 15, 10, 30, tzinfo=UTC)

        self.assertEqual(get_current_status(times, now=now), "🔴 Немає світла")

    def test_naive_injected_times_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "aware datetime"):
            get_current_status({"00:00": "0"}, now=datetime(2026, 1, 15, 12, 0))
        with self.assertRaisesRegex(ValueError, "aware datetime"):
            PowerService.render_schedule_caption(
                {"00:00": "0"},
                "3.2",
                date(2026, 1, 15),
                datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
                now=datetime(2026, 1, 15, 12, 0),
            )

    @unittest.skipUnless(hasattr(time, "tzset"), "time.tzset() is unavailable")
    def test_current_status_is_independent_of_host_timezone(self) -> None:
        times = {"00:00": "0", "12:00": "1", "18:00": "10"}
        now = datetime(2026, 7, 15, 10, 30, tzinfo=UTC)
        original_tz = os.environ.get("TZ")
        original_host_offset = now.astimezone().utcoffset()
        results: list[str] = []
        captions: list[str] = []
        host_offsets: list[timedelta | None] = []

        try:
            for host_zone in ("Pacific/Kiritimati", "America/Los_Angeles"):
                os.environ["TZ"] = host_zone
                time.tzset()
                host_offsets.append(now.astimezone().utcoffset())
                results.append(get_current_status(times, now=now))
                captions.append(
                    PowerService.render_schedule_caption(
                        times,
                        "3.2",
                        date(2026, 7, 15),
                        datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
                        now=now,
                    )
                )
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

        self.assertEqual(host_offsets, [timedelta(hours=14), -timedelta(hours=7)])
        self.assertEqual(results, ["🔴 Немає світла", "🔴 Немає світла"])
        self.assertEqual(captions[0], captions[1])
        self.assertIn("⚡️ **Зараз:** 🔴 Немає світла", captions[0])
        self.assertEqual(now.astimezone().utcoffset(), original_host_offset)


class HandlerKyivDateTests(unittest.IsolatedAsyncioTestCase):
    async def call_handler(
        self, handler: Callable[[types.Message], Awaitable[None]], now: datetime
    ) -> datetime:
        service = SimpleNamespace(get_formatted_schedule=AsyncMock(return_value=("schedule", True)))
        message = types.Message.model_construct(
            message_id=1,
            date=now,
            chat=types.Chat(id=123, type="private"),
            from_user=types.User(id=123, is_bot=False, first_name="Test"),
        )
        with (
            patch.object(handlers, "PowerService", return_value=service),
            patch.object(handlers, "kyiv_now", return_value=as_kyiv(now)),
            patch.object(types.Message, "answer", new_callable=AsyncMock),
        ):
            await handler(message)

        service.get_formatted_schedule.assert_awaited_once()
        requested = service.get_formatted_schedule.await_args.args[1]
        assert isinstance(requested, datetime)
        self.assertIs(requested.tzinfo, KYIV_TZ)
        return requested

    async def test_today_shortly_before_kyiv_midnight(self) -> None:
        requested = await self.call_handler(
            handlers.get_today_schedule,
            datetime(2026, 1, 15, 21, 59, tzinfo=UTC),
        )

        self.assertEqual(requested.date(), date(2026, 1, 15))
        self.assertEqual((requested.hour, requested.minute), (23, 59))

    async def test_tomorrow_after_kyiv_midnight_while_utc_is_previous_date(self) -> None:
        utc_now = datetime(2026, 7, 1, 21, 5, tzinfo=UTC)
        requested = await self.call_handler(handlers.get_tomorrow_schedule, utc_now)

        self.assertEqual(utc_now.date(), date(2026, 7, 1))
        self.assertEqual(requested.date(), date(2026, 7, 3))
        self.assertEqual((requested.hour, requested.minute), (0, 5))
