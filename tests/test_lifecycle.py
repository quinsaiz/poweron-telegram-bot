import asyncio
import importlib
import os
import runpy
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import uvicorn

with patch.dict(os.environ, {"BOT_TOKEN": "123:test-only-token"}):
    from src import main


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

        async def monitor(_bot):
            self.monitor_task = asyncio.current_task()
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
        self.patches.enter_context(patch.object(main, "init_db", new_callable=AsyncMock))
        self.patches.enter_context(patch.object(main, "check_updates_loop", monitor))

    async def test_shutdown_stops_polling_cancels_scheduler_and_closes_resources(self):
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
        async def failed_monitor(_bot):
            raise ValueError("scheduler failed")

        self.patches.enter_context(patch.object(main, "check_updates_loop", failed_monitor))

        with self.assertRaises(ExceptionGroup) as caught:
            async with main.lifespan(main.app):
                await self.dispatcher.started.wait()
                await asyncio.sleep(0)

        self.assertTrue(any(isinstance(error, ValueError) for error in caught.exception.exceptions))
        self.session.close.assert_awaited_once_with()
        self.engine.dispose.assert_awaited_once_with()


class DirectLaunchTests(unittest.TestCase):
    def test_module_launch_uses_environment_port_and_default(self):
        bot_module = importlib.import_module("src.telegram.bot")
        fake_dispatcher = SimpleNamespace(
            include_router=Mock(), message=SimpleNamespace(middleware=Mock())
        )

        for env_port, expected_port in (("8765", 8765), (None, 9999)):
            with self.subTest(port=env_port):
                env = {"BOT_TOKEN": "123:test-only-token"}
                if env_port is not None:
                    env["PORT"] = env_port
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(bot_module, "dp", fake_dispatcher),
                    patch.object(uvicorn, "run") as uvicorn_run,
                ):
                    namespace = runpy.run_path(main.__file__, run_name="__main__")

                uvicorn_run.assert_called_once_with(
                    namespace["app"], host="0.0.0.0", port=expected_port
                )
