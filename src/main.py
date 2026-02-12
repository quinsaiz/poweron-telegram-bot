import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager

from src.config import settings
from src.logger import setup_logger
from src.telegram.handlers import router as telegram_router
from src.telegram.bot import bot, dp
from src.database.engine import init_db
from src.poweron.scheduler import check_updates_loop

logger = setup_logger(__name__, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Init DB")
    await init_db()

    logger.info("Create tasks")
    polling_task = asyncio.create_task(dp.start_polling(bot))
    monitor_task = asyncio.create_task(check_updates_loop())
    yield

    logger.info("Stop poweron-telegram")
    polling_task.cancel()
    monitor_task.cancel()
    await bot.session.close()


app = FastAPI(title="Power Schedule Laskivtsi", lifespan=lifespan)

dp.include_router(telegram_router)
