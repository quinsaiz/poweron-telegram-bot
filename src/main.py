import asyncio
from collections.abc import AsyncIterator  # noqa: TC003  # runtime lifespan reflection
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from aiogram.utils.chat_action import ChatActionMiddleware
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from src.config import settings
from src.database.engine import engine
from src.database.errors import TRANSIENT_DATABASE_EXCEPTIONS
from src.logger import setup_logger
from src.poweron.groups import group_resolver
from src.poweron.scheduler import StartupGroupRefreshState, check_updates_loop
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


class ComponentState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"


@dataclass(frozen=True, slots=True)
class WorkerCompletion:
    exception: BaseException | None
    cancelled: bool
    logged: bool


type WorkerName = Literal["polling", "scheduler"]


@dataclass(slots=True)
class RuntimeHealth:
    startup_completed: bool = False
    shutdown_started: bool = False
    polling_task: asyncio.Task[Any] | None = None
    scheduler_task: asyncio.Task[Any] | None = None
    _completions: dict[WorkerName, WorkerCompletion] = field(default_factory=dict)

    def set_worker(self, name: WorkerName, task: asyncio.Task[Any]) -> None:
        if name == "polling":
            self.polling_task = task
        else:
            self.scheduler_task = task
        task.add_done_callback(lambda _task: self._observe_completion(name))

    def _worker_task(self, name: WorkerName) -> asyncio.Task[Any] | None:
        return self.polling_task if name == "polling" else self.scheduler_task

    def _observe_completion(self, name: WorkerName) -> WorkerCompletion | None:
        task = self._worker_task(name)
        if task is None or not task.done():
            return None
        if completion := self._completions.get(name):
            return completion

        cancelled = task.cancelled()
        exception = None if cancelled else task.exception()
        logged = False
        label = "Telegram polling" if name == "polling" else "Scheduler"
        if not self.shutdown_started:
            logged = True
            if exception is None:
                logger.error("%s stopped unexpectedly", label)
            else:
                logger.error("%s failed unexpectedly", label, exc_info=exception)
        completion = WorkerCompletion(
            exception=exception,
            cancelled=cancelled,
            logged=logged,
        )
        self._completions[name] = completion
        return completion

    def worker_completion(self, name: WorkerName) -> WorkerCompletion | None:
        return self._observe_completion(name)

    def worker_state(self, name: WorkerName) -> ComponentState:
        if self.shutdown_started:
            return ComponentState.SHUTTING_DOWN
        task = self._worker_task(name)
        if task is None:
            return ComponentState.STARTING
        if not task.done():
            return ComponentState.RUNNING
        completion = self._observe_completion(name)
        if completion is not None and completion.exception is not None:
            return ComponentState.FAILED
        return ComponentState.STOPPED

    def readiness(self) -> tuple[bool, dict[str, str]]:
        application = (
            ComponentState.SHUTTING_DOWN
            if self.shutdown_started
            else ComponentState.RUNNING
            if self.startup_completed
            else ComponentState.STARTING
        )
        components = {
            "application": application.value,
            "telegram_polling": self.worker_state("polling").value,
            "scheduler": self.worker_state("scheduler").value,
        }
        ready = (
            self.startup_completed
            and not self.shutdown_started
            and components["telegram_polling"] == ComponentState.RUNNING
            and components["scheduler"] == ComponentState.RUNNING
        )
        return ready, components


dp.include_router(telegram_router)
dp.message.middleware(AntiFloodMiddleware(limit=10, window=10, ban_time=300))
dp.message.middleware(ChatActionMiddleware())


def _task_result(
    task: asyncio.Task[Any],
    name: str,
    errors: list[Exception],
    *,
    cancelled: bool,
    completion: WorkerCompletion | None = None,
) -> None:
    if completion is not None:
        if completion.cancelled:
            if not cancelled:
                error = RuntimeError(f"{name} was cancelled unexpectedly")
                if not completion.logged:
                    logger.error("%s", error)
                errors.append(error)
        elif isinstance(completion.exception, Exception):
            if not completion.logged:
                logger.error("%s failed", name, exc_info=completion.exception)
            errors.append(completion.exception)
        return
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


def _log_late_worker_result(runtime_health: RuntimeHealth, worker: WorkerName, name: str) -> None:
    completion = runtime_health.worker_completion(worker)
    if completion is not None and completion.exception is not None and not completion.logged:
        logger.error(
            "%s failed after shutdown timeout",
            name,
            exc_info=completion.exception,
        )


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
    tasks: dict[str, asyncio.Task[Any]],
    cancelled: set[str],
    errors: list[Exception],
    runtime_health: RuntimeHealth,
) -> None:
    for name, task in tasks.items():
        if task.done():
            if name == "Polling stop request":
                _stop_request_result(task, errors, cancelled=name in cancelled)
            else:
                worker: WorkerName = "polling" if name == "Polling task" else "scheduler"
                _task_result(
                    task,
                    name,
                    errors,
                    cancelled=name in cancelled,
                    completion=runtime_health.worker_completion(worker),
                )
        else:

            def log_late_result(finished: asyncio.Task[Any], label: str = name) -> None:
                if label == "Polling stop request":
                    _log_late_task_result(finished, label)
                else:
                    worker: WorkerName = "polling" if label == "Polling task" else "scheduler"
                    _log_late_worker_result(runtime_health, worker, label)

            task.add_done_callback(log_late_result)


async def _finish_workers(
    polling_task: asyncio.Task[Any] | None,
    monitor_task: asyncio.Task[Any] | None,
    errors: list[Exception],
    runtime_health: RuntimeHealth,
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
    _collect_worker_results(tasks, cancelled, errors, runtime_health)

    if pending:
        names = ", ".join(name for name, task in tasks.items() if task in pending)
        error = TimeoutError(f"Worker shutdown deadline exceeded: {names}")
        logger.critical("%s; resources remain open", error)
        raise ExceptionGroup("Bot shutdown failed", [*errors, error])


async def _shutdown(
    polling_task: asyncio.Task[Any] | None,
    monitor_task: asyncio.Task[Any] | None,
    runtime_health: RuntimeHealth,
) -> None:
    logger.info("Stopping bot services...")
    runtime_health.shutdown_started = True
    errors: list[Exception] = []
    await _finish_workers(polling_task, monitor_task, errors, runtime_health)

    await _close_resource(bot.session.close(), "Bot session", errors)
    await _close_resource(engine.dispose(), "Database engine", errors)

    if errors:
        raise ExceptionGroup("Bot shutdown failed", errors)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    runtime_health = RuntimeHealth()
    _.state.runtime_health = runtime_health
    polling_task = None
    monitor_task = None
    try:
        logger.info("Starting bot services...")
        await group_resolver.ensure_source_identity()
        startup_group_refresh_state = StartupGroupRefreshState.SUCCEEDED
        try:
            await group_resolver.ensure_group()
        except asyncio.CancelledError:
            raise
        except TRANSIENT_DATABASE_EXCEPTIONS:
            logger.exception("Initial PowerOn group refresh failed; scheduler will retry")
            startup_group_refresh_state = StartupGroupRefreshState.TRANSIENT_FAILURE
        polling_task = asyncio.create_task(
            dp.start_polling(
                bot,
                drop_pending_updates=True,
                handle_signals=False,
                close_bot_session=False,
            ),
        )
        runtime_health.set_worker("polling", polling_task)
        monitor_task = asyncio.create_task(
            check_updates_loop(
                bot,
                startup_group_refresh_state=startup_group_refresh_state,
            )
        )
        runtime_health.set_worker("scheduler", monitor_task)
        runtime_health.startup_completed = True
        yield
    finally:
        await _shutdown(polling_task, monitor_task, runtime_health)


app = FastAPI(title="poweron-telegram-bot", lifespan=lifespan)
app.state.runtime_health = RuntimeHealth()


def _readiness_response(request: Request, *, root: bool = False) -> JSONResponse:
    runtime_health: RuntimeHealth = request.app.state.runtime_health
    ready, components = runtime_health.readiness()
    if root:
        content: dict[str, Any] = {
            "status": "ok" if ready else "unavailable",
            "bot": components["telegram_polling"],
            "scheduler": components["scheduler"],
        }
    else:
        content = {"status": "ready" if ready else "not_ready", "components": components}
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=content,
    )


@app.get("/")
async def health_check(request: Request) -> JSONResponse:
    return _readiness_response(request, root=True)


@app.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    return _readiness_response(request)
