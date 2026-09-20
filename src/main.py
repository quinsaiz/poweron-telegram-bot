import asyncio
from collections.abc import AsyncIterator  # noqa: TC003  # runtime lifespan reflection
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from aiogram.utils.chat_action import ChatActionMiddleware
from fastapi import FastAPI

from src.config import settings
from src.database.engine import engine, init_db
from src.logger import setup_logger
from src.poweron.scheduler import check_updates_loop
from src.telegram.bot import bot, dp
from src.telegram.handlers import router as telegram_router
from src.telegram.middlewares import AntiFloodMiddleware

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = setup_logger(__name__, settings.LOG_LEVEL)

POLLING_STOP_TIMEOUT = 5.0
WORKER_SHUTDOWN_TIMEOUT = 10.0
TASK_WAIT_TIMEOUT = 1.0
RESOURCE_CLOSE_TIMEOUT = 1.0


dp.include_router(telegram_router)
dp.message.middleware(AntiFloodMiddleware(limit=10, window=10, ban_time=300))
dp.message.middleware(ChatActionMiddleware())


def _task_result(
    task: asyncio.Task[Any], name: str, errors: list[Exception], *, cancelled: bool
) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        if not cancelled:
            error = RuntimeError(f"{name} was cancelled unexpectedly")
            logger.error("%s", error)
            errors.append(error)
    except Exception as error:
        logger.error("%s failed", name, exc_info=error)
        errors.append(error)


def _log_late_task_result(task: asyncio.Task[Any], name: str) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("%s failed after shutdown timeout", name, exc_info=error)


async def _wait_for_task(
    task: asyncio.Task[Any],
    name: str,
    errors: list[Exception],
    *,
    wait_seconds: float,
    cancelled: bool = False,
) -> None:
    done, _ = await asyncio.wait({task}, timeout=wait_seconds)
    if not done:
        logger.warning("%s did not finish within %.1f seconds; cancelling", name, wait_seconds)
        task.cancel()
        cancelled = True
        done, _ = await asyncio.wait({task}, timeout=TASK_WAIT_TIMEOUT)
    if done:
        _task_result(task, name, errors, cancelled=cancelled)
    else:
        error = TimeoutError(f"{name} did not stop after cancellation")
        logger.error("%s", error)
        errors.append(error)
        task.add_done_callback(lambda finished: _log_late_task_result(finished, name))


async def _close_resource(
    coro: Coroutine[Any, Any, Any], name: str, errors: list[Exception]
) -> None:
    task = asyncio.create_task(coro)
    await _wait_for_task(task, name, errors, wait_seconds=RESOURCE_CLOSE_TIMEOUT)


def _stop_request_result(
    stop_task: asyncio.Task[Any], errors: list[Exception], *, cancelled: bool
) -> None:
    try:
        stop_task.result()
    except asyncio.CancelledError:
        if not cancelled:
            error = RuntimeError("Polling stop request was cancelled unexpectedly")
            logger.error("%s", error)
            errors.append(error)
    except RuntimeError as error:
        # Startup can finish before the polling task acquires aiogram's lock.
        if str(error) != "Polling is not started":
            logger.error("Polling stop request failed", exc_info=error)
            errors.append(error)
    except Exception as error:
        logger.error("Polling stop request failed", exc_info=error)
        errors.append(error)


def _collect_worker_results(
    tasks: dict[str, asyncio.Task[Any]], cancelled: set[str], errors: list[Exception]
) -> None:
    for name, task in tasks.items():
        if task.done():
            if name == "Polling stop request":
                _stop_request_result(task, errors, cancelled=name in cancelled)
            else:
                _task_result(task, name, errors, cancelled=name in cancelled)
        else:

            def log_late_result(finished: asyncio.Task[Any], label: str = name) -> None:
                _log_late_task_result(finished, label)

            task.add_done_callback(log_late_result)


async def _finish_workers(
    polling_task: asyncio.Task[Any] | None,
    monitor_task: asyncio.Task[Any] | None,
    errors: list[Exception],
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WORKER_SHUTDOWN_TIMEOUT
    tasks: dict[str, asyncio.Task[Any]] = {}
    cancelled: set[str] = set()

    if polling_task is not None:
        tasks["Polling task"] = polling_task
        if not polling_task.done():
            tasks["Polling stop request"] = asyncio.create_task(dp.stop_polling())
    if monitor_task is not None:
        tasks["Scheduler task"] = monitor_task
        if not monitor_task.done():
            monitor_task.cancel()
            cancelled.add("Scheduler task")

    if not tasks:
        return

    cooperative_seconds = min(POLLING_STOP_TIMEOUT, WORKER_SHUTDOWN_TIMEOUT / 2)
    await asyncio.wait(tasks.values(), timeout=cooperative_seconds)
    for name, task in tasks.items():
        if not task.done():
            logger.warning("%s did not stop cooperatively; cancelling", name)
            task.cancel()
            cancelled.add(name)

    _, pending = await asyncio.wait(tasks.values(), timeout=max(0, deadline - loop.time()))
    _collect_worker_results(tasks, cancelled, errors)

    if pending:
        names = ", ".join(name for name, task in tasks.items() if task in pending)
        error = TimeoutError(f"Worker shutdown deadline exceeded: {names}")
        logger.critical("%s; resources remain open", error)
        raise ExceptionGroup("Bot shutdown failed", [*errors, error])


async def _shutdown(
    polling_task: asyncio.Task[Any] | None,
    monitor_task: asyncio.Task[Any] | None,
) -> None:
    logger.info("Stopping bot services...")
    errors: list[Exception] = []
    await _finish_workers(polling_task, monitor_task, errors)

    await _close_resource(bot.session.close(), "Bot session", errors)
    await _close_resource(engine.dispose(), "Database engine", errors)

    if errors:
        raise ExceptionGroup("Bot shutdown failed", errors)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    polling_task = None
    monitor_task = None
    try:
        logger.info("Starting bot services...")
        await init_db()
        polling_task = asyncio.create_task(
            dp.start_polling(
                bot,
                drop_pending_updates=True,
                handle_signals=False,
                close_bot_session=False,
            ),
        )
        monitor_task = asyncio.create_task(check_updates_loop(bot))
        yield
    finally:
        await _shutdown(polling_task, monitor_task)


app = FastAPI(title="poweron-telegram-bot", lifespan=lifespan)


@app.get("/")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "bot": "running"}
