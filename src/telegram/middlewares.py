import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message


class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, limit: int = 10, window: int = 10, ban_time: int = 300):
        self.users: Dict[int, list[float]] = {}
        self.banned_users: Dict[int, float] = {}
        self.limit = limit
        self.window = window
        self.ban_time = ban_time
        super().__init__()

    async def __call__(
            self,
            handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        now = time.time()

        if user_id in self.banned_users:
            if now < self.banned_users[user_id]:
                return None
            else:
                del self.banned_users[user_id]

        if user_id not in self.users:
            self.users[user_id] = []

        self.users[user_id] = [t for t in self.users[user_id] if now - t < self.window]

        self.users[user_id].append(now)

        if len(self.users[user_id]) > self.limit:
            self.banned_users[user_id] = now + self.ban_time
            await event.answer(
                f"❌ **Ви заблоковані на {self.ban_time // 60} хв за спам!**",
                parse_mode="Markdown",
            )
            return None

        return await handler(event, data)
