import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

with patch.dict(os.environ, {"BOT_TOKEN": "test-only-token"}):
    from src.poweron import service as service_module

from src.database.models import Base, ScheduleCache, User


class ScheduleCacheTests(unittest.IsolatedAsyncioTestCase):
    date = datetime(2024, 1, 15, 12)
    date_str = "2024-01-15"
    chat_id = 12345
    group = "3.2"

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session_patch = patch.object(service_module, "async_session", self.session)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.service = service_module.PowerService()
        self.requests = []

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def add_user(self, group="3.2"):
        async with self.session() as session:
            session.add(User(chat_id=self.chat_id, group=group))
            await session.commit()

    async def add_cache(self, date_str=None, group="3.2", age_minutes=31):
        async with self.session() as session:
            session.add(
                ScheduleCache(
                    date_graph=date_str or self.date_str,
                    group=group,
                    times_json=json.dumps({"00:00": "0"}),
                    updated_at=datetime.now(timezone.utc)
                    - timedelta(minutes=age_minutes),
                )
            )
            await session.commit()

    def mock_api(self, *, times=None, error=False, date_graph=None, group=None):
        real_client = httpx.AsyncClient

        def respond(request):
            self.requests.append(request)
            if error:
                raise httpx.ConnectError("upstream unavailable", request=request)
            return httpx.Response(
                200,
                json={
                    "hydra:member": [
                        {
                            "id": 1,
                            "dateGraph": f"{date_graph or self.date_str}T00:00:00+03:00",
                            "dataJson": {group or self.group: {"times": times}},
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(respond)
        client_patch = patch.object(
            service_module.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def isolated_api(self, *, times, group):
        real_client = httpx.AsyncClient
        payload = {
            "hydra:member": [
                {
                    "id": 1,
                    "dateGraph": f"{self.date_str}T00:00:00+03:00",
                    "dataJson": {
                        "3.2": {"times": {"00:00": "0"}},
                        group: {"times": dict(times)},
                    },
                }
            ]
        }

        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(respond)
        return patch.object(
            service_module.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
        )

    async def test_fresh_cache_does_not_request_upstream(self):
        await self.add_user()
        await self.add_cache(age_minutes=1)
        self.mock_api(error=True)

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("🟢 Є світло", text)
        self.assertEqual(self.requests, [])

    async def test_expired_cache_is_refreshed_and_persisted(self):
        await self.add_user()
        await self.add_cache()
        self.mock_api(times={"00:00": "1", "12:30": "10"})

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("🔴 Немає світла", text)
        self.assertIn("🟡 Перемикання", text)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(
            self.requests[0].url.params["after"], "2024-01-14T12:00:00+00:00"
        )
        self.assertEqual(
            self.requests[0].url.params["before"], "2024-01-16T12:00:00+00:00"
        )
        async with self.session() as session:
            cache = (await session.execute(select(ScheduleCache))).scalar_one()
        self.assertEqual(json.loads(cache.times_json), {"00:00": "1", "12:30": "10"})

    async def test_expired_cache_is_used_when_upstream_fails(self):
        await self.add_user()
        await self.add_cache()
        self.mock_api(error=True)

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("🟢 Є світло", text)
        self.assertEqual(len(self.requests), 1)

    async def test_failed_commit_rolls_back_before_stale_fallback(self):
        await self.add_user()
        await self.add_cache()
        self.mock_api(times={"00:00": "1"})
        async with self.session() as session:
            original = (await session.execute(select(ScheduleCache))).scalar_one()
            original_updated_at = original.updated_at

        failed_commit = []
        rollbacks = []
        original_rollback = AsyncSession.rollback
        original_lookup = self.service.get_schedule_from_cache

        async def fail_commit(session):
            await session.flush()
            failed_commit.append(True)
            raise OperationalError("UPDATE schedule_cache", {}, Exception("simulated"))

        async def track_rollback(session):
            rollbacks.append(True)
            await original_rollback(session)

        async def check_lookup(*args, **kwargs):
            if failed_commit:
                self.assertTrue(rollbacks)
            return await original_lookup(*args, **kwargs)

        with (
            patch.object(AsyncSession, "commit", fail_commit),
            patch.object(AsyncSession, "rollback", track_rollback),
            patch.object(self.service, "get_schedule_from_cache", check_lookup),
        ):
            text, ok = await self.service.get_formatted_schedule(
                self.chat_id, self.date
            )

        self.assertTrue(ok)
        self.assertIn("🟢 Є світло", text)
        self.assertNotIn("🔴 Немає світла", text)
        self.assertEqual(len(rollbacks), 1)
        async with self.session() as session:
            cache = (await session.execute(select(ScheduleCache))).scalar_one()
        self.assertEqual(cache.group, self.group)
        self.assertEqual(json.loads(cache.times_json), {"00:00": "0"})
        self.assertEqual(cache.updated_at, original_updated_at)

    async def test_failed_commit_without_stale_cache_keeps_no_schedule_result(self):
        await self.add_user()
        self.mock_api(times={"00:00": "1"})

        async def fail_commit(session):
            await session.flush()
            raise OperationalError("INSERT schedule_cache", {}, Exception("simulated"))

        with patch.object(AsyncSession, "commit", fail_commit):
            text, ok = await self.service.get_formatted_schedule(
                self.chat_id, self.date
            )

        self.assertFalse(ok)
        self.assertEqual(text, "❌ **Графіка на 15 січня ще немає**")
        async with self.session() as session:
            rows = (await session.execute(select(ScheduleCache))).scalars().all()
        self.assertEqual(rows, [])

    async def test_cold_cache_preserves_no_schedule_result_on_failure(self):
        await self.add_user()
        self.mock_api(error=True)

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertFalse(ok)
        self.assertEqual(text, "❌ **Графіка на 15 січня ще немає**")
        self.assertEqual(len(self.requests), 1)

    async def test_stale_cache_never_crosses_group_or_date(self):
        await self.add_user(group="4.1")
        await self.add_cache(group="3.2")
        await self.add_cache(date_str="2024-01-14", group="4.1")
        self.mock_api(error=True)

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertFalse(ok)
        self.assertEqual(text, "❌ **Графіка на 15 січня ще немає**")

    async def test_unusable_upstream_data_uses_matching_stale_cache(self):
        await self.add_user()
        await self.add_cache()
        self.mock_api(times={})

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("🟢 Є світло", text)
        self.assertEqual(len(self.requests), 1)

    async def test_malformed_time_does_not_replace_stale_cache(self):
        await self.add_user()
        await self.add_cache()
        self.mock_api(times={"00:00": "1", "not-a-time": "1"})

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("🟢 Є світло", text)
        async with self.session() as session:
            cache = (await session.execute(select(ScheduleCache))).scalar_one()
        self.assertEqual(json.loads(cache.times_json), {"00:00": "0"})

    async def test_24_00_start_is_rejected_without_cache_save(self):
        await self.add_user()
        await self.add_cache()
        self.mock_api(times={"24:00": "1"})

        with patch.object(
            self.service, "save_schedule_to_cache", new_callable=AsyncMock
        ) as save_cache:
            text, ok = await self.service.get_formatted_schedule(
                self.chat_id, self.date
            )

        self.assertTrue(ok)
        self.assertIn("🟢 Є світло", text)
        save_cache.assert_not_awaited()
        async with self.session() as session:
            cache = (await session.execute(select(ScheduleCache))).scalar_one()
        self.assertEqual(json.loads(cache.times_json), {"00:00": "0"})

    async def test_23_59_start_remains_valid(self):
        await self.add_user()
        self.mock_api(times={"23:59": "1"})

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("23:59 — 24:00", text)
        async with self.session() as session:
            cache = (await session.execute(select(ScheduleCache))).scalar_one()
        self.assertEqual(json.loads(cache.times_json), {"23:59": "1"})

    async def test_invalid_start_keys_never_replace_stale_cache(self):
        target_group = "4.1"
        await self.add_user(group=target_group)
        invalid_keys = ("0:00", "00:0", "23:60", "24:00", "٠٠:٠٠", "１２:００")
        for key in invalid_keys:
            with self.subTest(start_key=key):
                self.requests.clear()
                async with self.session() as session:
                    await session.execute(delete(ScheduleCache))
                    await session.commit()
                await self.add_cache(group=target_group)
                async with self.session() as session:
                    stale = (await session.execute(select(ScheduleCache))).scalar_one()
                    original = (
                        stale.id,
                        stale.date_graph,
                        stale.group,
                        stale.times_json,
                        stale.updated_at,
                    )

                with patch.object(
                    self.service, "get_schedule", new_callable=AsyncMock
                ) as no_refresh:
                    no_refresh.return_value = None
                    expected = await self.service.get_formatted_schedule(
                        self.chat_id, self.date
                    )
                self.assertTrue(expected[1])
                self.assertIn("📅 **Графік на 15 січня**", expected[0])
                self.assertIn("🏘 Група: **4.1**", expected[0])
                self.assertIn("`00:00 — 24:00:` 🟢 Є світло", expected[0])

                times = {"00:00": "1", key: "1"}
                with self.isolated_api(times=times, group=target_group):
                    with patch.object(
                        self.service, "save_schedule_to_cache", new_callable=AsyncMock
                    ) as save_cache:
                        save_cache.reset_mock()
                        result = await self.service.get_schedule(
                            group=target_group, date=self.date
                        )
                        self.assertIsNone(result)
                        with patch.object(
                            self.service, "get_schedule", new_callable=AsyncMock
                        ) as no_refresh:
                            no_refresh.return_value = None
                            actual = await self.service.get_formatted_schedule(
                                self.chat_id, self.date
                            )
                            no_refresh.assert_awaited_once_with(
                                group=target_group, date=self.date
                            )
                        save_cache.assert_not_called()

                self.assertEqual(len(self.requests), 1)
                request = self.requests[0]
                self.assertEqual(
                    request.url.params["after"], "2024-01-14T12:00:00+00:00"
                )
                self.assertEqual(
                    request.url.params["before"], "2024-01-16T12:00:00+00:00"
                )
                self.assertEqual(
                    request.url.params["time"], str(service_module.settings.CITY_ID)
                )
                self.assertEqual(actual, expected)
                async with self.session() as session:
                    stale = (
                        await session.execute(
                            select(ScheduleCache).where(
                                ScheduleCache.date_graph == self.date_str,
                                ScheduleCache.group == target_group,
                            )
                        )
                    ).scalar_one()
                self.assertEqual(
                    (
                        stale.id,
                        stale.date_graph,
                        stale.group,
                        stale.times_json,
                        stale.updated_at,
                    ),
                    original,
                )

    async def test_valid_start_boundaries_are_persisted(self):
        target_group = "4.1"
        await self.add_user(group=target_group)
        for key in ("00:00", "09:05", "23:59"):
            with self.subTest(start_key=key):
                self.requests.clear()
                async with self.session() as session:
                    await session.execute(delete(ScheduleCache))
                    await session.commit()
                times = {key: "1"}
                with self.isolated_api(times=times, group=target_group):
                    text, ok = await self.service.get_formatted_schedule(
                        self.chat_id, self.date
                    )

                self.assertTrue(ok)
                self.assertIn("🏘 Група: **4.1**", text)
                self.assertIn(f"`{key} — 24:00:` 🔴 Немає світла", text)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(
                    self.requests[0].url.params["after"],
                    "2024-01-14T12:00:00+00:00",
                )
                self.assertEqual(
                    self.requests[0].url.params["before"],
                    "2024-01-16T12:00:00+00:00",
                )
                async with self.session() as session:
                    cache = (await session.execute(select(ScheduleCache))).scalar_one()
                self.assertEqual(cache.date_graph, self.date_str)
                self.assertEqual(cache.group, target_group)
                self.assertEqual(json.loads(cache.times_json), {key: "1"})

    async def test_unsupported_status_does_not_replace_stale_cache(self):
        await self.add_user()
        await self.add_cache()
        self.mock_api(times={"00:00": "1", "12:30": "9"})

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("🟢 Є світло", text)
        async with self.session() as session:
            cache = (await session.execute(select(ScheduleCache))).scalar_one()
        self.assertEqual(json.loads(cache.times_json), {"00:00": "0"})

    async def test_other_date_from_upstream_does_not_fill_cold_cache(self):
        await self.add_user()
        self.mock_api(times={"00:00": "1"}, date_graph="2024-01-14")

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertFalse(ok)
        self.assertEqual(text, "❌ **Графіка на 15 січня ще немає**")
        async with self.session() as session:
            rows = (await session.execute(select(ScheduleCache))).scalars().all()
        self.assertEqual(rows, [])

    async def test_successful_refresh_replaces_other_group_for_same_date(self):
        await self.add_user()
        await self.add_cache(group="4.1")
        self.mock_api(times={"00:00": "1"})

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("🔴 Немає світла", text)
        async with self.session() as session:
            cache = (await session.execute(select(ScheduleCache))).scalar_one()
        self.assertEqual(cache.group, self.group)
        self.assertEqual(json.loads(cache.times_json), {"00:00": "1"})

    async def test_refresh_selects_non_default_user_group(self):
        await self.add_user(group="4.1")
        self.mock_api(times={"00:00": "1"}, group="4.1")

        text, ok = await self.service.get_formatted_schedule(self.chat_id, self.date)

        self.assertTrue(ok)
        self.assertIn("Група: **4.1**", text)
        self.assertIn("🔴 Немає світла", text)
        async with self.session() as session:
            cache = (await session.execute(select(ScheduleCache))).scalar_one()
        self.assertEqual(cache.group, "4.1")
        self.assertEqual(json.loads(cache.times_json), {"00:00": "1"})
