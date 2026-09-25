from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

with patch.dict(
    os.environ,
    {
        "BOT_TOKEN": "test-only-token",
        "POWERON_CITY_ID": "21005",
        "POWERON_API_URL": "https://api-poweron.toe.com.ua/api",
    },
):
    from src.database.models import Base, PowerOnSourceState
    from src.poweron.groups import PowerOnGroupResolver, SourceIdentityError


class GroupResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.engine.dispose)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        self.requests: list[httpx.Request] = []
        self.payload: object = {"buildingGroups": [{"chergGpv": "2.2"}]}
        self.status = 200
        self.content_type = "application/json"

    def resolver(self, handler=None) -> PowerOnGroupResolver:
        def default_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                self.status,
                json=self.payload,
                headers={"content-type": self.content_type},
            )

        return PowerOnGroupResolver(
            session_factory=self.session,
            clock=lambda: self.now,
            transport=httpx.MockTransport(handler or default_handler),
        )

    async def state(self) -> PowerOnSourceState:
        async with self.session() as session:
            state = await session.get(PowerOnSourceState, 1)
            self.assertIsNotNone(state)
            assert state is not None
            return state

    async def seed(
        self,
        *,
        city_id: int = 21005,
        group: str | None = None,
        attempt: datetime | None = None,
        success: datetime | None = None,
    ) -> None:
        async with self.session() as session, session.begin():
            session.add(
                PowerOnSourceState(
                    id=1,
                    city_id=city_id,
                    group=group,
                    last_refresh_attempt_at=attempt,
                    last_successful_refresh_at=success,
                )
            )

    async def test_cold_success_binds_city_and_persists_group(self) -> None:
        resolver = self.resolver()

        self.assertEqual(await resolver.ensure_group(), "2.2")

        state = await self.state()
        self.assertEqual(state.city_id, 21005)
        self.assertEqual(state.group, "2.2")
        self.assertEqual(state.last_refresh_attempt_at.replace(tzinfo=UTC), self.now)
        self.assertEqual(state.last_successful_refresh_at.replace(tzinfo=UTC), self.now)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(
            str(self.requests[0].url.copy_with(query=None)),
            "https://api-poweron.toe.com.ua/api/pw-accounts/building-groups",
        )
        self.assertEqual(self.requests[0].url.params["cityId"], "21005")
        self.assertEqual(self.requests[0].headers["X-debug-key"], "MjEwMDU=")

    async def test_fresh_success_is_reused_for_one_hour(self) -> None:
        resolver = self.resolver()
        self.assertEqual(await resolver.ensure_group(), "2.2")
        self.now += timedelta(minutes=59, seconds=59)

        self.assertEqual(await resolver.ensure_group(), "2.2")
        self.assertEqual(len(self.requests), 1)

    async def test_refreshes_after_one_hour_and_replaces_group(self) -> None:
        resolver = self.resolver()
        self.assertEqual(await resolver.ensure_group(), "2.2")
        self.now += timedelta(hours=1)
        self.payload = {"buildingGroups": [{"chergGpv": "4.1"}]}

        self.assertEqual(await resolver.ensure_group(), "4.1")
        self.assertEqual((await self.state()).group, "4.1")
        self.assertEqual(len(self.requests), 2)

    async def test_failed_attempt_is_throttled_for_ten_minutes(self) -> None:
        self.status = 503
        resolver = self.resolver()

        self.assertIsNone(await resolver.ensure_group())
        self.now += timedelta(minutes=9, seconds=59)
        self.assertIsNone(await resolver.ensure_group())
        self.assertEqual(len(self.requests), 1)

        self.now += timedelta(seconds=1)
        self.assertIsNone(await resolver.ensure_group())
        self.assertEqual(len(self.requests), 2)

    async def test_every_invalid_response_retains_last_known_group(self) -> None:
        failure_cases = (
            ("content type", {"buildingGroups": [{"chergGpv": "4.1"}]}, "text/html"),
            ("malformed", {"wrong": []}, "application/json"),
            ("invalid group", {"buildingGroups": [{"chergGpv": "bad"}]}, "application/json"),
            ("empty", {"buildingGroups": []}, "application/json"),
            (
                "ambiguous",
                {"buildingGroups": [{"chergGpv": "4.1"}, {"chergGpv": "2.2"}]},
                "application/json",
            ),
        )
        for name, payload, content_type in failure_cases:
            with self.subTest(name=name):
                async with self.session() as session, session.begin():
                    state = await session.get(PowerOnSourceState, 1)
                    if state is None:
                        session.add(
                            PowerOnSourceState(
                                id=1,
                                city_id=21005,
                                group="3.3",
                                last_refresh_attempt_at=self.now - timedelta(hours=2),
                                last_successful_refresh_at=self.now - timedelta(hours=2),
                            )
                        )
                    else:
                        state.group = "3.3"
                        state.last_refresh_attempt_at = self.now - timedelta(hours=2)
                        state.last_successful_refresh_at = self.now - timedelta(hours=2)
                self.payload = payload
                self.content_type = content_type
                resolver = self.resolver()

                self.assertEqual(await resolver.ensure_group(), "3.3")
                self.assertEqual((await self.state()).group, "3.3")
                self.now += timedelta(hours=2)

    async def test_network_error_retains_last_known_group(self) -> None:
        await self.seed(
            group="3.3",
            attempt=self.now - timedelta(hours=2),
            success=self.now - timedelta(hours=2),
        )

        def fail(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            raise httpx.ConnectError("offline", request=request)

        self.assertEqual(await self.resolver(fail).ensure_group(), "3.3")
        self.assertEqual((await self.state()).group, "3.3")

    async def test_timeout_and_malformed_json_retain_last_known_group(self) -> None:
        failure_handlers = (
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)),
            lambda request: httpx.Response(
                200,
                content=b"{not-json",
                headers={"content-type": "application/json"},
                request=request,
            ),
        )
        for handler in failure_handlers:
            with self.subTest(handler=handler):
                async with self.session() as session, session.begin():
                    state = await session.get(PowerOnSourceState, 1)
                    if state is None:
                        session.add(
                            PowerOnSourceState(
                                id=1,
                                city_id=21005,
                                group="3.3",
                                last_refresh_attempt_at=self.now - timedelta(hours=2),
                                last_successful_refresh_at=self.now - timedelta(hours=2),
                            )
                        )
                    else:
                        state.last_refresh_attempt_at = self.now - timedelta(hours=2)
                        state.last_successful_refresh_at = self.now - timedelta(hours=2)

                self.assertEqual(await self.resolver(handler).ensure_group(), "3.3")
                self.assertEqual((await self.state()).group, "3.3")
                self.now += timedelta(hours=2)

    async def test_cancelled_request_propagates_without_recording_attempt(self) -> None:
        entered = asyncio.Event()

        async def wait_forever(_request: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        task = asyncio.create_task(self.resolver(wait_forever).ensure_group())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        state = await self.state()
        self.assertIsNone(state.group)
        self.assertIsNone(state.last_refresh_attempt_at)

    async def test_overlapping_calls_make_one_request(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(
                200,
                json={"buildingGroups": [{"chergGpv": "2.2"}]},
                headers={"content-type": "application/json"},
            )

        resolver = self.resolver(respond)
        first = asyncio.create_task(resolver.ensure_group())
        await entered.wait()
        second = asyncio.create_task(resolver.ensure_group())
        release.set()

        self.assertEqual(await asyncio.gather(first, second), ["2.2", "2.2"])
        self.assertEqual(len(self.requests), 1)

    async def test_http_occurs_without_open_database_transaction(self) -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            async with self.session() as session, session.begin():
                state = await session.get(PowerOnSourceState, 1)
                self.assertIsNotNone(state)
            return httpx.Response(
                200,
                json={"buildingGroups": [{"chergGpv": "2.2"}]},
                headers={"content-type": "application/json"},
            )

        self.assertEqual(await self.resolver(respond).ensure_group(), "2.2")

    async def test_source_identity_accepts_match_and_rejects_mismatch(self) -> None:
        resolver = self.resolver()
        await resolver.ensure_source_identity()
        await resolver.ensure_source_identity()
        self.assertEqual((await self.state()).city_id, 21005)

        async with self.session() as session, session.begin():
            state = await session.get(PowerOnSourceState, 1)
            assert state is not None
            state.city_id = 999

        with self.assertRaisesRegex(SourceIdentityError, "remove and recreate"):
            await resolver.ensure_source_identity()
        self.assertEqual((await self.state()).city_id, 999)
