import asyncio
from datetime import datetime
from aiogram import Bot
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramNotFound,
)
from sqlalchemy import select, delete

from src.config import settings
from src.logger import setup_logger
from src.database.engine import async_session
from src.database.models import ScheduleState, User
from src.poweron.service import PowerService

logger = setup_logger(__name__, settings.LOG_LEVEL)


async def send_notification(bot: Bot, date: datetime):
    service = PowerService()

    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()

    logger.info(f"Start sending notifications to {len(users)} users...")

    success_count = 0
    for user in users:
        try:
            text, ok = await service.get_formatted_schedule(user.chat_id, date)

            if ok:
                notification = f"🔔 **ОПУБЛІКОВАНО ОНОВЛЕННЯ!**\n\n{text}"
                await bot.send_message(
                    user.chat_id, notification, parse_mode="Markdown"
                )
                success_count += 1

            await asyncio.sleep(0.05)

        except (TelegramForbiddenError, TelegramNotFound):
            async with async_session() as session:
                await session.execute(delete(User).where(User.chat_id == user.chat_id))
                await session.commit()

        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)

        except Exception as e:
            logger.error(f"Failed to send message {user.chat_id}: {e}")

    logger.info(f"✅ Sending completed. Successfully: {success_count}")


async def check_updates_loop(bot: Bot):
    service = PowerService()

    while True:
        try:
            logger.info("Checking for schedule updates...")
            result = await service.get_schedule(group=settings.DEFAULT_GROUP)

            if not result or not result.events:
                logger.info("The schedule is empty or unavailable.")
                await asyncio.sleep(600)
                continue

            if result and result.events:
                latest_event = result.events[0]
                new_id = latest_event.id

                async with async_session() as session:
                    state_res = await session.execute(select(ScheduleState))
                    state = state_res.scalar_one_or_none()

                    if state is None:
                        session.add(ScheduleState(last_id=new_id))
                        await session.commit()

                        logger.info(f"Initial state saved with ID: {new_id}")
                    elif new_id != state.last_id:
                        logger.info(
                            f"New schedule detected! Old ID: {state.last_id}, New ID: {new_id}"
                        )

                        state.last_id = new_id
                        await session.commit()

                        date_str = latest_event.date_graph.split("T")[0]
                        target_date = datetime.strptime(date_str, "%Y-%m-%d")
                        await send_notification(bot, target_date)
                    else:
                        logger.info(f"No new schedule. Current ID: {state.last_id}")
            else:
                logger.info("The schedule is empty or unavailable.")

        except Exception as e:
            logger.error(f"Monitoring error: {e}")

        await asyncio.sleep(600)
