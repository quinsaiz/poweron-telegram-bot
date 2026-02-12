import asyncio
from sqlalchemy import select

from src.config import settings
from src.logger import setup_logger
from src.database.engine import async_session
from src.database.models import ScheduleState
from src.poweron.service import PowerService

logger = setup_logger(__name__, settings.LOG_LEVEL)


async def check_updates_loop():
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
                    elif new_id > state.last_id:
                        logger.info(f"New schedule detected! Old ID: {state.last_id}, New ID: {new_id}")
                        state.last_id = new_id
                        await session.commit()
                        logger.info("Cache updated via get_schedule()")
                    else:
                        logger.info(f"No new schedule. Current ID: {state.last_id}")

        except Exception as e:
            logger.error(f"Monitoring error: {e}")

        await asyncio.sleep(600)
