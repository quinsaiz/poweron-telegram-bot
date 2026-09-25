import asyncio
import inspect
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, patch

import httpx
from aiogram import Bot
from sqlalchemy.exc import ArgumentError, InvalidRequestError, OperationalError

with patch.dict(
    os.environ,
    {
        "BOT_TOKEN": "123:test-only-token",
        "POWERON_CITY_ID": "21005",
        "POWERON_API_URL": "https://api-poweron.toe.com.ua/api",
    },
):
    from src import main
    from src.poweron.groups import SourceIdentityError
    from src.poweron.scheduler import (
        ScheduleScheduler,
        StartupGroupRefreshState,
        check_updates_loop,
    )
    from src.telegram.middlewares import AntiFloodMiddleware


class FakeDispatcher:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.polling_finished = asyncio.Event()
        self.options = None
        self.stop_calls = 0
        self.polling_task = None

    async def start_polling(self, _bot, **options):
        self.options = options
        self.polling_task = asyncio.current_task()
        self.started.set()
        try:
            await self.stopped.wait()
        finally:
            self.polling_finished.set()

    async def stop_polling(self):
        self.stop_calls += 1
        self.stopped.set()
        await self.polling_finished.wait()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.dispatcher = FakeDispatcher()
        self.session = SimpleNamespace(close=AsyncMock())
        self.engine = SimpleNamespace(dispose=AsyncMock())
        self.monitor_started = asyncio.Event()
        self.monitor_finished = asyncio.Event()
        self.monitor_task = None
        self.monitor_refresh_state = None

        async def monitor(_bot, *, startup_group_refresh_state):
            self.monitor_task = asyncio.current_task()
            self.monitor_refresh_state = startup_group_refresh_state
            self.monitor_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.monitor_finished.set()

        self.patches.enter_context(patch.object(main, "dp", self.dispatcher))
        self.patches.enter_context(
            patch.object(main, "bot", SimpleNamespace(session=self.session)),
        )
        self.patches.enter_context(patch.object(main, "engine", self.engine))
        self.patches.enter_context(patch.object(main, "check_updates_loop", monitor))
        self.ensure_source_identity = self.patches.enter_context(
            patch.object(main.group_resolver, "ensure_source_identity", new_callable=AsyncMock)
        )
        self.ensure_group = self.patches.enter_context(
            patch.object(
                main.group_resolver,
                "ensure_group",
                new_callable=AsyncMock,
                return_value="2.2",
            )
        )

    async def test_shutdown_stops_polling_cancels_scheduler_and_closes_resources(self):
        with self.assertNoLogs(main.logger, level="ERROR"):
            async with main.lifespan(main.app):
                await self.dispatcher.started.wait()
                await self.monitor_started.wait()
                self.assertFalse(self.monitor_task.done())

        self.assertFalse(self.dispatcher.options["handle_signals"])
        self.assertFalse(self.dispatcher.options["close_bot_session"])
        self.assertEqual(self.dispatcher.stop_calls, 1)
        self.assertTrue(self.dispatcher.polling_finished.is_set())
        self.assertTrue(self.dispatcher.polling_task.done())
        self.assertTrue(self.monitor_finished.is_set())
        self.assertTrue(self.monitor_task.done())
        self.assertTrue(self.monitor_task.cancelled())
        self.ensure_group.assert_awaited_once_with()
        self.assertIs(self.monitor_refresh_state, StartupGroupRefreshState.SUCCEEDED)
        self.session.close.assert_awaited_once_with()
        self.engine.dispose.assert_awaited_once_with()

    async def test_source_mismatch_fails_before_workers_start(self):
        self.ensure_source_identity.side_effect = SourceIdentityError("configured city mismatch")

        with self.assertRaisesRegex(SourceIdentityError, "city mismatch"):
            async with main.lifespan(main.app):
                self.fail("lifespan must not start")

        self.assertFalse(self.dispatcher.started.is_set())
        self.assertFalse(self.monitor_started.is_set())
        self.ensure_group.assert_not_awaited()

    async def test_transient_initial_group_state_failure_still_starts_workers_and_retries(self):
        failure = OperationalError("SELECT source state", {}, Exception("temporary"))
        self.ensure_group.side_effect = [failure, "2.2"]
        wait_started = asyncio.Event()
        release_wait = asyncio.Event()
        discovery_resumed = asyncio.Event()
        pending_scan = AsyncMock(return_value=0)

        async def controlled_wait(_delay: float) -> None:
            wait_started.set()
            await release_wait.wait()

        async def resumed_discovery() -> int:
            discovery_resumed.set()
            await asyncio.Event().wait()
            return 0

        async def retrying_monitor(_bot, *, startup_group_refresh_state):
            self.monitor_task = asyncio.current_task()
            self.monitor_refresh_state = startup_group_refresh_state
            self.monitor_started.set()
            try:
                scheduler = ScheduleScheduler(
                    _bot,
                    service=SimpleNamespace(group_resolver=main.group_resolver),
                    sleep=controlled_wait,
                )
                with (
                    patch.object(scheduler, "process_due_deliveries", pending_scan),
                    patch.object(scheduler, "discover_events", side_effect=resumed_discovery),
                ):
                    await scheduler.run(startup_group_refresh_state)
            finally:
                self.monitor_finished.set()

        self.patches.enter_context(patch.object(main, "check_updates_loop", retrying_monitor))

        with self.assertLogs(main.logger, level="ERROR") as logs:
            async with main.lifespan(main.app):
                await self.dispatcher.started.wait()
                await self.monitor_started.wait()
                await wait_started.wait()
                self.assertIs(
                    self.monitor_refresh_state,
                    StartupGroupRefreshState.TRANSIENT_FAILURE,
                )
                self.assertEqual(self.ensure_group.await_count, 1)
                pending_scan.assert_awaited_once_with()
                release_wait.set()
                await discovery_resumed.wait()
                self.assertEqual(self.ensure_group.await_count, 2)

        self.assertIn("Initial PowerOn group refresh failed", "\n".join(logs.output))
        self.assertTrue(self.dispatcher.polling_finished.is_set())
        self.assertTrue(self.monitor_finished.is_set())
        self.session.close.assert_awaited_once_with()
        self.engine.dispose.assert_awaited_once_with()

    async def test_initial_group_refresh_cancellation_is_not_swallowed(self):
        self.ensure_group.side_effect = asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            async with main.lifespan(main.app):
                self.fail("lifespan must not start")

        self.assertFalse(self.dispatcher.started.is_set())
        self.assertFalse(self.monitor_started.is_set())
        self.session.close.assert_awaited_once_with()
        self.engine.dispose.assert_awaited_once_with()

    async def test_invalid_request_during_initial_group_refresh_is_fatal(self):
        self.ensure_group.side_effect = InvalidRequestError("broken session state")

        with self.assertRaisesRegex(InvalidRequestError, "broken session state"):
            async with main.lifespan(main.app):
                self.fail("lifespan must not start")

        self.assertFalse(self.dispatcher.started.is_set())
        self.assertFalse(self.monitor_started.is_set())
        self.session.close.assert_awaited_once_with()
        self.engine.dispose.assert_awaited_once_with()

    async def test_argument_error_during_initial_group_refresh_is_fatal(self):
        self.ensure_group.side_effect = ArgumentError("invalid statement construction")

        with self.assertRaisesRegex(ArgumentError, "invalid statement construction"):
            async with main.lifespan(main.app):
                self.fail("lifespan must not start")

        self.assertFalse(self.dispatcher.started.is_set())
        self.assertFalse(self.monitor_started.is_set())
        self.session.close.assert_awaited_once_with()
        self.engine.dispose.assert_awaited_once_with()

    async def test_shutdown_is_bounded_when_polling_ignores_stop_request(self):
        async def stuck_stop():
            self.dispatcher.stop_calls += 1
            await asyncio.Event().wait()

        self.dispatcher.stop_polling = stuck_stop
        self.patches.enter_context(patch.object(main, "POLLING_STOP_TIMEOUT", 0.01))
        self.patches.enter_context(patch.object(main, "WORKER_SHUTDOWN_TIMEOUT", 0.06))
        loop = asyncio.get_running_loop()
        warnings = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: warnings.append(context))
        self.addCleanup(loop.set_exception_handler, previous_handler)

        async def run_lifespan():
            async with main.lifespan(main.app):
                await self.dispatcher.started.wait()
                await self.monitor_started.wait()

        await asyncio.wait_for(run_lifespan(), timeout=0.5)
        await asyncio.sleep(0)

        self.assertEqual(self.dispatcher.stop_calls, 1)
        self.assertTrue(self.dispatcher.polling_finished.is_set())
        self.assertTrue(self.dispatcher.polling_task.done())
        self.assertTrue(self.monitor_finished.is_set())
        self.session.close.assert_awaited_once_with()
        self.engine.dispose.assert_awaited_once_with()
        self.assertEqual(warnings, [])

    async def test_pending_worker_is_fatal_and_resources_stay_open(self):
        release = asyncio.Event()

        async def stubborn_polling(_bot, **options):
            self.dispatcher.options = options
            self.dispatcher.polling_task = asyncio.current_task()
            self.dispatcher.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                self.dispatcher.polling_finished.set()

        self.dispatcher.start_polling = stubborn_polling
        self.patches.enter_context(patch.object(main, "POLLING_STOP_TIMEOUT", 0.01))
        self.patches.enter_context(patch.object(main, "WORKER_SHUTDOWN_TIMEOUT", 0.06))

        async def run_lifespan():
            async with main.lifespan(main.app):
                await self.dispatcher.started.wait()
                await self.monitor_started.wait()

        try:
            with (
                self.assertLogs(main.logger, level="CRITICAL") as logs,
                self.assertRaises(ExceptionGroup) as caught,
            ):
                await asyncio.wait_for(run_lifespan(), timeout=0.5)

            self.assertTrue(
                any(isinstance(error, TimeoutError) for error in caught.exception.exceptions)
            )
            self.assertIn("Worker shutdown deadline exceeded", logs.output[0])
            self.assertFalse(self.dispatcher.polling_task.done())
            self.assertTrue(self.monitor_task.done())
            self.session.close.assert_not_awaited()
            self.engine.dispose.assert_not_awaited()
        finally:
            release.set()
            if self.dispatcher.polling_task is not None:
                await asyncio.wait_for(self.dispatcher.polling_task, timeout=0.5)

        self.assertTrue(self.dispatcher.polling_task.done())

    async def test_unexpected_scheduler_failure_is_reported_after_cleanup(self):
        async def failed_monitor(_bot, *, startup_group_refresh_state):
            self.assertIs(startup_group_refresh_state, StartupGroupRefreshState.SUCCEEDED)
            raise ValueError("scheduler failed")

        self.patches.enter_context(patch.object(main, "check_updates_loop", failed_monitor))

        with self.assertRaises(ExceptionGroup) as caught:
            async with main.lifespan(main.app):
                await self.dispatcher.started.wait()
                await asyncio.sleep(0)

        self.assertTrue(any(isinstance(error, ValueError) for error in caught.exception.exceptions))
        self.session.close.assert_awaited_once_with()
        self.engine.dispose.assert_awaited_once_with()


class RuntimeHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_health = main.app.state.runtime_health
        main.app.state.runtime_health = main.RuntimeHealth()
        self.addAsyncCleanup(self._restore_health)

    async def _restore_health(self):
        health = main.app.state.runtime_health
        health.shutdown_started = True
        for task in (health.polling_task, health.scheduler_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [
            task
            for task in (health.polling_task, health.scheduler_task)
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        main.app.state.runtime_health = self.previous_health

    async def _get(self, path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    @staticmethod
    async def _wait_forever(started: asyncio.Event) -> None:
        started.set()
        await asyncio.Event().wait()

    @staticmethod
    async def _finish_on_release(started: asyncio.Event, release: asyncio.Event) -> None:
        started.set()
        await release.wait()

    async def _running_health(self) -> main.RuntimeHealth:
        health = main.RuntimeHealth(startup_completed=True)
        polling_started = asyncio.Event()
        scheduler_started = asyncio.Event()
        health.set_worker("polling", asyncio.create_task(self._wait_forever(polling_started)))
        health.set_worker("scheduler", asyncio.create_task(self._wait_forever(scheduler_started)))
        main.app.state.runtime_health = health
        await polling_started.wait()
        await scheduler_started.wait()
        return health

    async def _health_with_completing_worker(
        self, worker: str
    ) -> tuple[main.RuntimeHealth, asyncio.Event]:
        health = main.RuntimeHealth(startup_completed=True)
        polling_started = asyncio.Event()
        scheduler_started = asyncio.Event()
        release = asyncio.Event()
        polling_coro = (
            self._finish_on_release(polling_started, release)
            if worker == "polling"
            else self._wait_forever(polling_started)
        )
        scheduler_coro = (
            self._finish_on_release(scheduler_started, release)
            if worker == "scheduler"
            else self._wait_forever(scheduler_started)
        )
        health.set_worker("polling", asyncio.create_task(polling_coro))
        health.set_worker("scheduler", asyncio.create_task(scheduler_coro))
        main.app.state.runtime_health = health
        await polling_started.wait()
        await scheduler_started.wait()
        return health, release

    async def test_liveness_is_independent_of_worker_readiness(self):
        response = await self._get("/health/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "alive"})

    async def test_readiness_is_unavailable_before_startup_completes(self):
        response = await self._get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["components"]["application"], "starting")

    async def test_running_workers_are_ready_and_root_is_truthful(self):
        await self._running_health()

        ready = await self._get("/health/ready")
        root = await self._get("/")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(
            root.json(),
            {"status": "ok", "bot": "running", "scheduler": "running"},
        )

    async def test_completed_polling_task_makes_readiness_unavailable(self):
        health, release = await self._health_with_completing_worker("polling")
        release.set()
        with self.assertLogs(main.logger, level="ERROR") as logs:
            await health.polling_task
            response = await self._get("/health/ready")
            root = await self._get("/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(root.status_code, 503)
        self.assertEqual(root.json()["bot"], "stopped")
        self.assertEqual(response.json()["components"]["telegram_polling"], "stopped")
        self.assertIn("Telegram polling stopped unexpectedly", "\n".join(logs.output))

    async def test_completed_scheduler_task_makes_readiness_unavailable(self):
        health, release = await self._health_with_completing_worker("scheduler")
        release.set()
        with self.assertLogs(main.logger, level="ERROR"):
            await health.scheduler_task
            response = await self._get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["components"]["scheduler"], "stopped")

    async def test_failed_worker_is_logged_once_and_detail_is_not_exposed(self):
        secret = "private-worker-detail"

        async def fail(started: asyncio.Event, release: asyncio.Event) -> None:
            started.set()
            await release.wait()
            raise ValueError(secret)

        health = main.RuntimeHealth(startup_completed=True)
        polling_started = asyncio.Event()
        scheduler_started = asyncio.Event()
        release = asyncio.Event()
        health.set_worker("polling", asyncio.create_task(self._wait_forever(polling_started)))
        failed_task = asyncio.create_task(fail(scheduler_started, release))
        health.set_worker("scheduler", failed_task)
        main.app.state.runtime_health = health
        await polling_started.wait()
        await scheduler_started.wait()

        release.set()
        with self.assertLogs(main.logger, level="ERROR") as logs:
            await asyncio.gather(failed_task, return_exceptions=True)
            response = await self._get("/health/ready")
            await self._get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["components"]["scheduler"], "failed")
        self.assertNotIn(secret, response.text)
        self.assertEqual(
            sum("Scheduler failed unexpectedly" in record for record in logs.output),
            1,
        )

    async def test_shutdown_in_progress_is_unavailable(self):
        health = await self._running_health()
        health.shutdown_started = True

        response = await self._get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["components"]["application"], "shutting_down")
        self.assertEqual(response.json()["components"]["scheduler"], "shutting_down")


class RuntimeAnnotationTests(unittest.TestCase):
    def test_lifespan_annotations_can_be_inspected(self):
        signature = inspect.signature(main.lifespan)
        self.assertIn("_", signature.parameters)
        self.assertIn("return", main.lifespan.__annotations__)

    def test_scheduler_annotations_can_be_evaluated(self):
        self.assertIs(get_type_hints(check_updates_loop)["bot"], Bot)
        self.assertIs(inspect.get_annotations(check_updates_loop, eval_str=True)["bot"], Bot)

    def test_middleware_annotations_can_be_evaluated(self):
        self.assertIn("handler", get_type_hints(AntiFloodMiddleware.__call__))
