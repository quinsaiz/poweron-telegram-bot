import time
from collections.abc import Awaitable, Callable  # noqa: TC003  # runtime annotation reflection
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import delete, select

from src.database.engine import async_session
from src.database.models import BannedUser


class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, limit: int = 10, window: int = 10, ban_time: int = 300):
        self.users: dict[int, list[float]] = {}
        self.banned_cache: dict[int, datetime] = {}
        self.limit = limit
        self.window = window
        self.ban_time = ban_time
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        now_ts = time.time()
        now_dt = datetime.now(UTC)

        if user_id in self.banned_cache:
            if now_dt < self.banned_cache[user_id]:
                return None
            else:
                del self.banned_cache[user_id]

        async with async_session() as session:
            result = await session.execute(select(BannedUser).where(BannedUser.chat_id == user_id))
            ban_record = result.scalar_one_or_none()

            if ban_record:
                until_date = ban_record.until_date
                if until_date.tzinfo is None:
                    until_date = until_date.replace(tzinfo=UTC)

                if now_dt < until_date:
                    self.banned_cache[user_id] = until_date
                    return None
                else:
                    await session.execute(delete(BannedUser).where(BannedUser.chat_id == user_id))
                    await session.commit()

            if user_id not in self.users:
                self.users[user_id] = []

            self.users[user_id] = [t for t in self.users[user_id] if now_ts - t < self.window]
            self.users[user_id].append(now_ts)

            if len(self.users[user_id]) > self.limit:
                ban_until = now_dt + timedelta(seconds=self.ban_time)

                self.banned_cache[user_id] = ban_until

                new_ban = BannedUser(chat_id=user_id, until_date=ban_until)
                await session.merge(new_ban)
                await session.commit()

                await event.answer(
                    f"❌ **Ви заблоковані на {self.ban_time // 60} хв за спам!**",
                    parse_mode="Markdown",
                )
                return None

        return await handler(event, data)
