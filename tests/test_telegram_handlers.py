from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

with patch.dict(
    os.environ,
    {
        "BOT_TOKEN": "test-only-token",
        "POWERON_CITY_ID": "21005",
        "POWERON_API_URL": "https://api-poweron.toe.com.ua/api",
    },
):
    from src.database.models import Base, User
    from src.poweron.service import GROUP_UNAVAILABLE_MESSAGE
    from src.telegram import handlers


class TelegramHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.engine.dispose)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.message = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )

    async def users(self) -> list[User]:
        async with self.session() as session:
            return list((await session.execute(select(User))).scalars().all())

    async def test_start_creates_user_and_reports_discovered_group(self) -> None:
        resolver = SimpleNamespace(ensure_group=AsyncMock(return_value="2.2"))
        service = SimpleNamespace(group_resolver=resolver)
        with (
            patch.object(handlers, "async_session", self.session),
            patch.object(handlers, "PowerService", return_value=service),
        ):
            await handlers.cmd_start(self.message)

        self.assertEqual([user.chat_id for user in await self.users()], [123])
        answer = self.message.answer.await_args.args[0]
        self.assertIn("визначена автоматично: **2.2**", answer)
        self.assertNotIn("3.2", answer)

    async def test_start_creates_user_and_reports_group_unavailability(self) -> None:
        resolver = SimpleNamespace(ensure_group=AsyncMock(return_value=None))
        service = SimpleNamespace(group_resolver=resolver)
        with (
            patch.object(handlers, "async_session", self.session),
            patch.object(handlers, "PowerService", return_value=service),
        ):
            await handlers.cmd_start(self.message)

        self.assertEqual([user.chat_id for user in await self.users()], [123])
        answer = self.message.answer.await_args.args[0]
        self.assertIn("тимчасово недоступна", answer)
        self.assertNotIn("3.2", answer)

    async def test_today_reports_distinct_group_unavailability(self) -> None:
        service = SimpleNamespace(
            get_formatted_schedule=AsyncMock(return_value=(GROUP_UNAVAILABLE_MESSAGE, False))
        )
        with patch.object(handlers, "PowerService", return_value=service):
            await handlers.get_today_schedule(self.message)

        self.message.answer.assert_awaited_once_with(
            GROUP_UNAVAILABLE_MESSAGE,
            parse_mode="Markdown",
        )

    async def test_successful_group_discovery_preserves_no_schedule_response(self) -> None:
        no_schedule = "❌ **Графіка на 24 вересня ще немає**"
        service = SimpleNamespace(
            get_formatted_schedule=AsyncMock(return_value=(no_schedule, False))
        )
        with patch.object(handlers, "PowerService", return_value=service):
            await handlers.get_tomorrow_schedule(self.message)

        self.message.answer.assert_awaited_once_with(no_schedule, parse_mode="Markdown")
