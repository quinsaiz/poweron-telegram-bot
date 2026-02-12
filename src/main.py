import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager
from aiogram.utils.chat_action import ChatActionMiddleware

from src.config import settings
from src.logger import setup_logger
from src.database.engine import init_db
from src.poweron.scheduler import check_updates_loop
from src.telegram.bot import bot, dp
from src.telegram.middlewares import AntiFloodMiddleware
from src.telegram.handlers import router as telegram_router

logger = setup_logger(__name__, settings.LOG_LEVEL)

dp.include_router(telegram_router)

dp.message.middleware(AntiFloodMiddleware(limit=10, window=10, ban_time=300))
dp.message.middleware(ChatActionMiddleware())


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Init DB")
    await init_db()

    logger.info("Create tasks")
    polling_task = asyncio.create_task(dp.start_polling(bot))
    monitor_task = asyncio.create_task(check_updates_loop(bot))
    yield

    logger.info("Stop poweron-telegram-bot")
    polling_task.cancel()
    monitor_task.cancel()
    try:
        await polling_task
        await monitor_task
    except asyncio.CancelledError:
        logger.info("Tasks cancelled successfully")
    await bot.session.close()


app = FastAPI(title="poweron-telegram-bot", lifespan=lifespan)
