import asyncio
import os
import uvicorn
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
    logger.info("Starting bot services...")
    await init_db()

    polling_task = asyncio.create_task(dp.start_polling(bot, drop_pending_updates=True))
    monitor_task = asyncio.create_task(check_updates_loop(bot))

    yield

    logger.info("Stopping bot services...")
    polling_task.cancel()
    monitor_task.cancel()
    try:
        await asyncio.gather(polling_task, monitor_task, return_exceptions=True)
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
    await bot.session.close()


app = FastAPI(title="poweron-telegram-bot", lifespan=lifespan)


@app.get("/")
async def health_check():
    return {"status": "ok", "bot": "running"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
