import asyncio
import json
import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from poweron_live_fixtures import (
    LIVE_DATE_GRAPH,
    LIVE_EVENT_DATE,
    LIVE_EVENT_ID,
    LIVE_GROUP,
    live_collection,
    live_half_hour_times,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

TEST_BOT_TOKEN = "123:test-only-token"

with patch.dict(os.environ, {"BOT_TOKEN": TEST_BOT_TOKEN}):
    from src.database.engine import configure_sqlite_foreign_keys
    from src.database.models import (
        Base,
        NotificationDelivery,
        ProcessedScheduleEvent,
        ScheduleCache,
        User,
    )
    from src.poweron.scheduler import (
        DeliveryStatus,
        EventPersistenceResult,
        PreparedEvent,
        ScheduleScheduler,
    )
    from src.poweron.schemas import ScheduleResponse
    from src.poweron.service import PowerService, ScheduleFetchResult


class SchedulerOutboxTests(unittest.IsolatedAsyncioTestCase):
    event_date = date(2026, 9, 22)
    date_graph = "2026-09-22T00:00:00+03:00"

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "outbox.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}", poolclass=NullPool)
        configure_sqlite_foreign_keys(self.engine)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)
        self.now = datetime(2026, 9, 22, 9, 0, 0, tzinfo=UTC)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
        self.service = PowerService()
        self.service.fetch_schedule = AsyncMock()  # type: ignore[method-assign]
        self.scheduler = ScheduleScheduler(
            self.bot,
            service=self.service,
            session_factory=self.session,
            clock=lambda: self.now,
        )

    def response(self, *events: dict[str, object]) -> ScheduleFetchResult:
        schedule = ScheduleResponse.model_validate({"hydra:member": list(events)})
        return ScheduleFetchResult(schedule, frozenset((self.event_date,)))

    @staticmethod
    def telegram_network_error(message: str = "network unavailable") -> TelegramNetworkError:
        return TelegramNetworkError(method=SendMessage(chat_id=10, text="test"), message=message)

    @staticmethod
    def aware(value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    def event(
        self,
        event_id: int | str,
        groups: dict[str, dict[str, str]] | None = None,
        *,
        date_graph: str | None = date_graph,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "id": event_id,
            "dataJson": {
                group: {"times": times}
                for group, times in (groups or {"3.2": {"00:00": "0"}}).items()
            },
        }
        if date_graph is not None:
            result["dateGraph"] = date_graph
        return result

    def prepared_event(self, event_id: int | str) -> PreparedEvent:
        fetch = self.response(self.event(event_id))
        self.assertIsNotNone(fetch.response)
        assert fetch.response is not None
        prepared = self.scheduler._prepare_event(
            fetch.response.events[0], {"3.2"}, fetch.relevant_dates, self.now
        )
        self.assertIsNotNone(prepared)
        assert prepared is not None
        return prepared

    async def add_users(self, *users: tuple[int, str]) -> None:
        async with self.session() as session, session.begin():
            session.add_all(User(chat_id=chat_id, group=group) for chat_id, group in users)

    async def events(self) -> list[ProcessedScheduleEvent]:
        async with self.session() as session:
            result = await session.execute(
                select(ProcessedScheduleEvent).order_by(ProcessedScheduleEvent.event_id)
            )
            return list(result.scalars().all())

    async def deliveries(self) -> list[NotificationDelivery]:
        async with self.session() as session:
            result = await session.execute(
                select(NotificationDelivery).order_by(NotificationDelivery.id)
            )
            return list(result.scalars().all())

    async def caches(self) -> list[ScheduleCache]:
        async with self.session() as session:
            result = await session.execute(
                select(ScheduleCache).order_by(ScheduleCache.date_graph, ScheduleCache.group)
            )
            return list(result.scalars().all())

    async def add_cache(self, group: str = "3.2", times: dict[str, str] | None = None) -> None:
        async with self.session() as session, session.begin():
            session.add(
                ScheduleCache(
                    date_graph=self.event_date.isoformat(),
                    group=group,
                    times_json=json.dumps(times or {"00:00": "10"}),
                    updated_at=self.now,
                )
            )

    async def seed_delivery(
        self,
        *,
        event_id: int | str = 1,
        chat_id: int = 10,
        attempt_count: int = 0,
        status: str = DeliveryStatus.PENDING.value,
        next_attempt_at: datetime | None = None,
        add_user: bool = True,
    ) -> None:
        async with self.session() as session, session.begin():
            stored_event_id = str(event_id)
            if await session.get(ProcessedScheduleEvent, stored_event_id) is None:
                session.add(
                    ProcessedScheduleEvent(
                        event_id=stored_event_id,
                        date_graph=self.date_graph,
                        created_at=self.now,
                    )
                )
                await session.flush()
            if add_user:
                session.add(User(chat_id=chat_id, group="3.2"))
            session.add(
                NotificationDelivery(
                    event_id=stored_event_id,
                    chat_id=chat_id,
                    recipient_group="3.2",
                    event_date=self.event_date.isoformat(),
                    message=f"message-{chat_id}",
                    status=status,
                    attempt_count=attempt_count,
                    next_attempt_at=next_attempt_at,
                    created_at=self.now,
                    updated_at=self.now,
                    sent_at=self.now if status == DeliveryStatus.SENT.value else None,
                    last_error=None,
                )
            )

    async def test_empty_response_creates_nothing(self) -> None:
        self.service.fetch_schedule.return_value = self.response()

        self.assertEqual(await self.scheduler.discover_events(), 0)
        self.assertEqual(await self.events(), [])
        self.assertEqual(await self.deliveries(), [])

    async def test_naive_scheduler_clock_is_rejected(self) -> None:
        self.scheduler.clock = lambda: datetime(2026, 4, 10, 12, 0)

        with self.assertRaisesRegex(ValueError, "Scheduler clock must return an aware datetime"):
            await self.scheduler.process_due_deliveries()

    async def test_sqlite_foreign_key_rejects_orphan_delivery(self) -> None:
        with self.assertRaises(IntegrityError):
            async with self.session() as session, session.begin():
                session.add(
                    NotificationDelivery(
                        event_id=999,
                        chat_id=10,
                        recipient_group="3.2",
                        event_date=self.event_date.isoformat(),
                        message="orphan",
                        status=DeliveryStatus.PENDING.value,
                        attempt_count=0,
                        next_attempt_at=self.now,
                        created_at=self.now,
                        updated_at=self.now,
                        sent_at=None,
                        last_error=None,
                    )
                )

    async def test_missing_date_creates_nothing(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(self.event(1, date_graph=None))

        self.assertEqual(await self.scheduler.discover_events(), 0)
        self.assertEqual(await self.events(), [])
        self.assertEqual(await self.deliveries(), [])

    async def test_date_relevance_uses_the_wire_calendar_date(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, date_graph="2026-09-22T00:00:00+00:00")
        )

        self.assertEqual(await self.scheduler.discover_events(), 1)
        self.assertEqual((await self.deliveries())[0].event_date, "2026-09-22")

    async def test_live_z_event_creates_outbox_delivery_cache_and_notification(self) -> None:
        self.now = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
        await self.add_users((10, LIVE_GROUP))
        schedule = ScheduleResponse.model_validate(live_collection())
        self.service.fetch_schedule.return_value = ScheduleFetchResult(
            schedule,
            frozenset((LIVE_EVENT_DATE,)),
        )

        self.assertEqual(await self.scheduler.discover_events(), 1)

        event = (await self.events())[0]
        self.assertEqual(event.event_id, str(LIVE_EVENT_ID))
        self.assertEqual(event.date_graph, LIVE_DATE_GRAPH)
        delivery = (await self.deliveries())[0]
        self.assertEqual(delivery.recipient_group, LIVE_GROUP)
        self.assertEqual(delivery.event_date, LIVE_EVENT_DATE.isoformat())
        self.assertIn("🔔 **ОПУБЛІКОВАНО ОНОВЛЕННЯ!**", delivery.message)
        self.assertIn(f"Група: **{LIVE_GROUP}**", delivery.message)
        self.assertIn("⚡️ **Зараз:**", delivery.message)
        cache = (await self.caches())[0]
        self.assertEqual(cache.date_graph, LIVE_EVENT_DATE.isoformat())
        self.assertEqual(cache.group, LIVE_GROUP)
        self.assertEqual(json.loads(cache.times_json), live_half_hour_times())

    async def test_missing_subscribed_group_creates_no_event_or_delivery(self) -> None:
        await self.add_users((10, "4.1"))
        self.service.fetch_schedule.return_value = self.response(self.event(1))

        self.assertEqual(await self.scheduler.discover_events(), 0)
        self.assertEqual(await self.events(), [])
        self.assertEqual(await self.deliveries(), [])

    async def test_malformed_time_or_status_creates_nothing(self) -> None:
        await self.add_users((10, "3.2"))
        invalid_schedules = ({"24:00": "0"}, {"00:00": "unknown"})
        for index, times in enumerate(invalid_schedules, start=1):
            with self.subTest(times=times):
                self.service.fetch_schedule.return_value = self.response(
                    self.event(index, {"3.2": times})
                )
                self.assertEqual(await self.scheduler.discover_events(), 0)

        self.assertEqual(await self.events(), [])
        self.assertEqual(await self.deliveries(), [])

    async def test_every_distinct_usable_event_is_processed(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(10), self.event(4), self.event(77)
        )

        self.assertEqual(await self.scheduler.discover_events(), 3)
        self.assertEqual({event.event_id for event in await self.events()}, {"10", "4", "77"})
        self.assertEqual(len(await self.deliveries()), 3)

    async def test_incomplete_member_does_not_hide_usable_sibling(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            {"id": "invalid", "dataJson": []}, self.event(8)
        )

        self.assertEqual(await self.scheduler.discover_events(), 1)
        self.assertEqual([event.event_id for event in await self.events()], ["8"])

    async def test_event_order_and_nonconsecutive_ids_do_not_change_processing(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            self.event("event-z"),
            self.event("event-a"),
            self.event("event-m"),
            self.event("event-a"),
        )

        await self.scheduler.discover_events()

        self.assertEqual(
            [event.event_id for event in await self.events()],
            ["event-a", "event-m", "event-z"],
        )

    async def test_malformed_duplicate_before_valid_duplicate_uses_valid_member(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, date_graph=None), self.event(1)
        )

        self.assertEqual(await self.scheduler.discover_events(), 1)
        self.assertEqual(len(await self.deliveries()), 1)

    async def test_valid_duplicate_before_malformed_duplicate_uses_valid_member(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1), self.event(1, date_graph="2026-W39-2")
        )

        self.assertEqual(await self.scheduler.discover_events(), 1)
        self.assertEqual(len(await self.deliveries()), 1)

    async def test_identical_valid_duplicates_are_processed_once(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(self.event(1), self.event(1))

        self.assertEqual(await self.scheduler.discover_events(), 1)
        self.assertEqual(len(await self.events()), 1)
        self.assertEqual(len(await self.deliveries()), 1)

    async def test_conflicting_valid_duplicates_are_skipped_atomically(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "0"}}),
            self.event(1, {"3.2": {"00:00": "1"}}),
        )

        self.assertEqual(await self.scheduler.discover_events(), 0)
        self.assertEqual(await self.events(), [])
        self.assertEqual(await self.deliveries(), [])
        self.assertEqual(await self.caches(), [])

    async def test_duplicate_poll_creates_no_duplicate_rows(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(self.event(1))

        self.assertEqual(await self.scheduler.discover_events(), 1)
        self.assertEqual(await self.scheduler.discover_events(), 0)
        self.assertEqual(len(await self.events()), 1)
        self.assertEqual(len(await self.deliveries()), 1)

    async def test_two_users_get_independent_deliveries(self) -> None:
        await self.add_users((10, "3.2"), (20, "3.2"))
        self.service.fetch_schedule.return_value = self.response(self.event(1))

        await self.scheduler.discover_events()

        rows = await self.deliveries()
        self.assertEqual({row.chat_id for row in rows}, {10, 20})
        self.assertEqual({row.status for row in rows}, {DeliveryStatus.PENDING.value})

    async def test_groups_receive_their_own_immutable_message_snapshots(self) -> None:
        await self.add_users((10, "3.2"), (20, "4.1"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "0"}, "4.1": {"00:00": "1"}})
        )

        await self.scheduler.discover_events()

        rows = {row.recipient_group: row for row in await self.deliveries()}
        self.assertIn("Група: **3.2**", rows["3.2"].message)
        self.assertIn("🟢 Є світло", rows["3.2"].message)
        self.assertIn("Група: **4.1**", rows["4.1"].message)
        self.assertIn("🔴 Немає світла", rows["4.1"].message)

    async def test_accepted_discovery_refreshes_existing_cache(self) -> None:
        await self.add_users((10, "3.2"))
        await self.add_cache(times={"00:00": "10"})
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "1"}})
        )

        await self.scheduler.discover_events()

        cache = (await self.caches())[0]
        self.assertEqual(json.loads(cache.times_json), {"00:00": "1"})

    async def test_persistence_results_cover_each_exclusive_state(self) -> None:
        await self.add_users((10, "3.2"))
        users = await self.scheduler._load_users()
        prepared = self.prepared_event(101)

        self.assertIs(
            await self.scheduler._persist_event(prepared, users, self.now),
            EventPersistenceResult.CREATED,
        )
        self.assertIs(
            await self.scheduler._persist_event(prepared, users, self.now),
            EventPersistenceResult.EXISTING_COMPATIBLE,
        )

        async with self.session() as session:
            stored = await session.get(ProcessedScheduleEvent, "101")
            self.assertIsNotNone(stored)
            assert stored is not None
            stored.date_graph = "2026-09-21T00:00:00+03:00"
            await session.commit()

        self.assertIs(
            await self.scheduler._persist_event(prepared, users, self.now),
            EventPersistenceResult.EXISTING_INCOMPATIBLE,
        )

        failed_prepared = self.prepared_event(102)

        async def fail_commit(_session: AsyncSession) -> None:
            raise OperationalError("COMMIT", {}, Exception("simulated"))

        with patch.object(AsyncSession, "commit", fail_commit):
            self.assertIs(
                await self.scheduler._persist_event(failed_prepared, users, self.now),
                EventPersistenceResult.FAILED,
            )

    def test_persistence_result_has_no_contradictory_boolean_state(self) -> None:
        self.assertEqual(
            set(EventPersistenceResult),
            {
                EventPersistenceResult.CREATED,
                EventPersistenceResult.EXISTING_COMPATIBLE,
                EventPersistenceResult.EXISTING_INCOMPATIBLE,
                EventPersistenceResult.FAILED,
            },
        )
        for result in EventPersistenceResult:
            self.assertFalse(hasattr(result, "cache_compatible"))
        with self.assertRaises(ValueError):
            EventPersistenceResult("created", True)

    async def test_discovery_caches_multiple_groups_independently(self) -> None:
        await self.add_users((10, "3.2"), (20, "4.1"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "0"}, "4.1": {"00:00": "1"}})
        )

        await self.scheduler.discover_events()

        self.assertEqual(
            {row.group: json.loads(row.times_json) for row in await self.caches()},
            {"3.2": {"00:00": "0"}, "4.1": {"00:00": "1"}},
        )

    async def test_invalid_discovery_schedule_does_not_overwrite_cache(self) -> None:
        await self.add_users((10, "3.2"), (20, "4.1"))
        await self.add_cache(group="4.1", times={"00:00": "0"})
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "1"}, "4.1": {"24:00": "1"}})
        )

        await self.scheduler.discover_events()

        self.assertEqual(
            {row.group: json.loads(row.times_json) for row in await self.caches()},
            {"3.2": {"00:00": "1"}, "4.1": {"00:00": "0"}},
        )

    async def test_immediate_cache_lookup_sees_discovered_schedule(self) -> None:
        await self.add_users((10, "3.2"))
        await self.add_cache(times={"00:00": "10"})
        self.now = datetime.now(UTC)
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "1"}})
        )
        await self.scheduler.discover_events()

        with patch("src.poweron.service.async_session", self.session):
            times, _ = await self.service.get_schedule_from_cache("2026-09-22", "3.2")

        self.assertEqual(times, {"00:00": "1"})

    async def test_notification_and_cache_share_the_accepted_snapshot(self) -> None:
        await self.add_users((10, "3.2"))
        times = {"00:00": "1", "12:30": "10"}
        self.service.fetch_schedule.return_value = self.response(self.event(1, {"3.2": times}))

        await self.scheduler.discover_events()

        delivery = (await self.deliveries())[0]
        cache = (await self.caches())[0]
        self.assertEqual(json.loads(cache.times_json), times)
        self.assertIn("🔴 Немає світла", delivery.message)
        self.assertIn("🟡 Перемикання", delivery.message)

    async def test_processed_event_refreshes_cache_without_duplicate_delivery(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "0"}})
        )
        await self.scheduler.discover_events()
        original_delivery = (await self.deliveries())[0]
        original_snapshot = (
            original_delivery.recipient_group,
            original_delivery.event_date,
            original_delivery.message,
        )
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "1"}})
        )

        self.assertEqual(await self.scheduler.discover_events(), 0)

        deliveries = await self.deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(
            (deliveries[0].recipient_group, deliveries[0].event_date, deliveries[0].message),
            original_snapshot,
        )
        self.assertEqual(json.loads((await self.caches())[0].times_json), {"00:00": "1"})

    async def test_persistence_failure_does_not_attempt_cache_upsert(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(self.event(1))

        async def fail_commit(_session: AsyncSession) -> None:
            raise OperationalError("COMMIT", {}, Exception("simulated"))

        with (
            patch.object(AsyncSession, "commit", fail_commit),
            patch.object(self.service, "upsert_schedule_cache", new_callable=AsyncMock) as upsert,
        ):
            self.assertEqual(await self.scheduler.discover_events(), 0)

        upsert.assert_not_awaited()
        self.assertEqual(await self.events(), [])
        self.assertEqual(await self.deliveries(), [])
        self.assertEqual(await self.caches(), [])

    async def test_uncertain_commit_does_not_advance_cache(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(self.event(1))
        original_commit = AsyncSession.commit

        async def commit_then_fail(session: AsyncSession) -> None:
            await original_commit(session)
            raise OperationalError("COMMIT", {}, Exception("outcome unknown"))

        with (
            patch.object(AsyncSession, "commit", commit_then_fail),
            patch.object(self.service, "upsert_schedule_cache", new_callable=AsyncMock) as upsert,
        ):
            self.assertEqual(await self.scheduler.discover_events(), 0)

        upsert.assert_not_awaited()
        self.assertEqual(len(await self.events()), 1)
        self.assertEqual(len(await self.deliveries()), 1)
        self.assertEqual(await self.caches(), [])

    async def test_failed_event_is_retried_on_later_poll(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(self.event(1))
        original_commit = AsyncSession.commit
        commit_calls = 0

        async def fail_once(session: AsyncSession) -> None:
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 1:
                raise OperationalError("COMMIT", {}, Exception("simulated"))
            await original_commit(session)

        with patch.object(AsyncSession, "commit", fail_once):
            self.assertEqual(await self.scheduler.discover_events(), 0)
            self.assertEqual(await self.scheduler.discover_events(), 1)

        self.assertEqual(len(await self.events()), 1)
        self.assertEqual(len(await self.deliveries()), 1)
        self.assertEqual(len(await self.caches()), 1)

    async def test_failed_event_does_not_block_independent_event(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "0"}}),
            self.event(2, {"3.2": {"00:00": "1"}}),
        )
        original_commit = AsyncSession.commit
        commit_calls = 0

        async def fail_first(session: AsyncSession) -> None:
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 1:
                raise OperationalError("COMMIT", {}, Exception("simulated"))
            await original_commit(session)

        with patch.object(AsyncSession, "commit", fail_first):
            self.assertEqual(await self.scheduler.discover_events(), 1)

        self.assertEqual([event.event_id for event in await self.events()], ["2"])
        self.assertEqual([row.event_id for row in await self.deliveries()], ["2"])
        self.assertEqual(json.loads((await self.caches())[0].times_json), {"00:00": "1"})

    async def test_incompatible_existing_event_does_not_refresh_cache(self) -> None:
        await self.add_users((10, "3.2"))
        await self.add_cache(times={"00:00": "10"})
        async with self.session() as session, session.begin():
            session.add(
                ProcessedScheduleEvent(
                    event_id="1",
                    date_graph="2026-09-21T00:00:00+03:00",
                    created_at=self.now,
                )
            )
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "1"}})
        )

        with patch.object(self.service, "upsert_schedule_cache", new_callable=AsyncMock) as upsert:
            self.assertEqual(await self.scheduler.discover_events(), 0)

        upsert.assert_not_awaited()
        self.assertEqual(len(await self.events()), 1)
        self.assertEqual(await self.deliveries(), [])
        self.assertEqual(json.loads((await self.caches())[0].times_json), {"00:00": "10"})

    async def test_cache_write_failure_does_not_lose_durable_outbox(self) -> None:
        await self.add_users((10, "3.2"), (20, "4.1"))
        self.service.fetch_schedule.return_value = self.response(
            self.event(1, {"3.2": {"00:00": "0"}, "4.1": {"00:00": "1"}})
        )
        original_upsert = self.service.upsert_schedule_cache

        async def fail_one_cache(session, date_graph, group, times, updated_at):
            if group == "3.2":
                raise OperationalError("cache upsert", {}, Exception("simulated"))
            await original_upsert(session, date_graph, group, times, updated_at)

        with patch.object(self.service, "upsert_schedule_cache", side_effect=fail_one_cache):
            self.assertEqual(await self.scheduler.discover_events(), 1)

        self.assertEqual(len(await self.events()), 1)
        self.assertEqual(len(await self.deliveries()), 2)
        self.assertEqual(
            {row.group: json.loads(row.times_json) for row in await self.caches()},
            {"4.1": {"00:00": "1"}},
        )

    async def test_valid_event_without_subscribers_is_recorded_once(self) -> None:
        self.service.fetch_schedule.return_value = self.response(self.event(1))

        self.assertEqual(await self.scheduler.discover_events(), 1)
        self.assertEqual(await self.scheduler.discover_events(), 0)
        self.assertEqual(len(await self.events()), 1)
        self.assertEqual(await self.deliveries(), [])

    async def test_success_is_recorded_only_after_send_returns(self) -> None:
        await self.seed_delivery()
        observed_statuses: list[str] = []

        async def send(*_args: object, **_kwargs: object) -> object:
            observed_statuses.append((await self.deliveries())[0].status)
            return object()

        self.bot.send_message.side_effect = send

        self.assertEqual(await self.scheduler.process_due_deliveries(), 1)

        delivery = (await self.deliveries())[0]
        self.assertEqual(observed_statuses, [DeliveryStatus.PENDING.value])
        self.assertEqual(delivery.status, DeliveryStatus.SENT.value)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(self.aware(delivery.sent_at), self.now)
        self.assertIsNone(delivery.next_attempt_at)

    async def test_one_failure_does_not_prevent_later_success(self) -> None:
        await self.seed_delivery(event_id=1, chat_id=10)
        await self.seed_delivery(event_id=2, chat_id=20)

        async def send(chat_id: int, *_args: object, **_kwargs: object) -> object:
            if chat_id == 10:
                raise self.telegram_network_error()
            return object()

        self.bot.send_message.side_effect = send

        self.assertEqual(await self.scheduler.process_due_deliveries(), 1)

        rows = {row.chat_id: row for row in await self.deliveries()}
        self.assertEqual(rows[10].status, DeliveryStatus.PENDING.value)
        self.assertEqual(rows[10].attempt_count, 1)
        self.assertEqual(rows[20].status, DeliveryStatus.SENT.value)
        self.assertIsNone(rows[20].next_attempt_at)

    async def test_retry_after_schedules_retry_without_blocking_later_user(self) -> None:
        await self.seed_delivery(event_id=1, chat_id=10)
        await self.seed_delivery(event_id=2, chat_id=20)

        async def send(chat_id: int, *_args: object, **_kwargs: object) -> object:
            if chat_id == 10:
                raise TelegramRetryAfter(
                    method=SendMessage(chat_id=chat_id, text="test"),
                    message="rate limited",
                    retry_after=37,
                )
            return object()

        self.bot.send_message.side_effect = send

        self.assertEqual(await self.scheduler.process_due_deliveries(), 1)

        rows = {row.chat_id: row for row in await self.deliveries()}
        self.assertEqual(rows[10].status, DeliveryStatus.PENDING.value)
        self.assertEqual(rows[10].attempt_count, 1)
        self.assertEqual(self.aware(rows[10].next_attempt_at), self.now + timedelta(seconds=37))
        self.assertEqual(rows[20].status, DeliveryStatus.SENT.value)

    async def test_transient_failure_retries_only_after_next_attempt_at(self) -> None:
        await self.seed_delivery()
        self.bot.send_message.side_effect = [
            self.telegram_network_error("temporary"),
            object(),
        ]

        await self.scheduler.process_due_deliveries()
        await self.scheduler.process_due_deliveries()
        self.assertEqual(self.bot.send_message.await_count, 1)

        self.now += timedelta(seconds=60)
        self.assertEqual(await self.scheduler.process_due_deliveries(), 1)
        delivery = (await self.deliveries())[0]
        self.assertEqual(delivery.status, DeliveryStatus.SENT.value)
        self.assertEqual(delivery.attempt_count, 2)
        self.assertIsNone(delivery.last_error)

    async def test_fifth_transient_attempt_becomes_exhausted(self) -> None:
        await self.seed_delivery(attempt_count=4)
        self.bot.send_message.side_effect = self.telegram_network_error("still unavailable")

        await self.scheduler.process_due_deliveries()

        delivery = (await self.deliveries())[0]
        self.assertEqual(delivery.status, DeliveryStatus.EXHAUSTED.value)
        self.assertEqual(delivery.attempt_count, 5)
        self.assertIn("TelegramNetworkError", delivery.last_error or "")
        self.assertIsNone(delivery.next_attempt_at)

    async def test_last_error_is_bounded_and_redacts_bot_token(self) -> None:
        await self.seed_delivery()
        self.bot.send_message.side_effect = self.telegram_network_error(
            f"request bot{TEST_BOT_TOKEN} {'x' * 1000}"
        )

        await self.scheduler.process_due_deliveries()

        last_error = (await self.deliveries())[0].last_error or ""
        self.assertLessEqual(len(last_error), 500)
        self.assertNotIn(TEST_BOT_TOKEN, last_error)
        self.assertNotIn(f"bot{TEST_BOT_TOKEN}", last_error)

    async def test_unexpected_local_exception_does_not_count_or_schedule_attempt(self) -> None:
        await self.seed_delivery()
        self.bot.send_message.side_effect = RuntimeError("local serialization bug")

        with self.assertLogs("src.poweron.scheduler", level="ERROR") as logs:
            await self.scheduler.process_due_deliveries()

        delivery = (await self.deliveries())[0]
        self.assertEqual(delivery.status, DeliveryStatus.PENDING.value)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertIsNone(delivery.next_attempt_at)
        self.assertIsNone(delivery.last_error)
        self.assertIn("Unexpected local error", "\n".join(logs.output))

    async def test_terminal_telegram_error_marks_terminal_and_removes_user(self) -> None:
        await self.seed_delivery()
        self.bot.send_message.side_effect = TelegramForbiddenError(
            method=SendMessage(chat_id=10, text="test"), message="bot was blocked"
        )

        await self.scheduler.process_due_deliveries()

        delivery = (await self.deliveries())[0]
        self.assertEqual(delivery.status, DeliveryStatus.TERMINAL.value)
        self.assertIsNone(delivery.next_attempt_at)
        async with self.session() as session:
            self.assertIsNone(
                (await session.execute(select(User).where(User.chat_id == 10))).scalar_one_or_none()
            )

    async def test_arbitrary_bad_request_is_recoverable_not_terminal(self) -> None:
        await self.seed_delivery()
        self.bot.send_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=10, text="test"), message="temporary malformed response"
        )

        await self.scheduler.process_due_deliveries()

        delivery = (await self.deliveries())[0]
        self.assertEqual(delivery.status, DeliveryStatus.PENDING.value)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertIsNotNone(delivery.next_attempt_at)
        async with self.session() as session:
            self.assertIsNotNone(
                (await session.execute(select(User).where(User.chat_id == 10))).scalar_one_or_none()
            )

    async def test_cancellation_leaves_delivery_recoverable(self) -> None:
        await self.seed_delivery()
        started = asyncio.Event()

        async def send(*_args: object, **_kwargs: object) -> object:
            started.set()
            await asyncio.Event().wait()
            return object()

        self.bot.send_message.side_effect = send
        task = asyncio.create_task(self.scheduler.process_due_deliveries())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        delivery = (await self.deliveries())[0]
        self.assertEqual(delivery.status, DeliveryStatus.PENDING.value)
        self.assertEqual(delivery.attempt_count, 0)

    async def test_null_next_attempt_is_immediately_due(self) -> None:
        await self.seed_delivery(next_attempt_at=None)

        self.assertEqual(await self.scheduler.process_due_deliveries(), 1)
        self.bot.send_message.assert_awaited_once()

    async def test_future_next_attempt_is_not_due(self) -> None:
        await self.seed_delivery(next_attempt_at=self.now + timedelta(minutes=1))

        self.assertEqual(await self.scheduler.process_due_deliveries(), 0)
        self.bot.send_message.assert_not_awaited()

    async def test_restart_resumes_pending_delivery(self) -> None:
        await self.add_users((10, "3.2"))
        self.service.fetch_schedule.return_value = self.response(self.event(1))
        await self.scheduler.discover_events()

        restarted_bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
        restarted = ScheduleScheduler(
            restarted_bot,
            service=self.service,
            session_factory=self.session,
            clock=lambda: self.now,
        )

        self.assertEqual(await restarted.process_due_deliveries(), 1)
        restarted_bot.send_message.assert_awaited_once()
        self.assertEqual((await self.deliveries())[0].status, DeliveryStatus.SENT.value)

    async def test_run_processes_pending_before_discovering_new_events(self) -> None:
        await self.seed_delivery()

        async def fetch() -> ScheduleFetchResult:
            self.assertEqual((await self.deliveries())[0].status, DeliveryStatus.SENT.value)
            raise asyncio.CancelledError

        self.service.fetch_schedule.side_effect = fetch

        with self.assertRaises(asyncio.CancelledError):
            await self.scheduler.run()
        self.bot.send_message.assert_awaited_once()

    async def test_startup_scan_failure_is_retried_without_stopping_scheduler(self) -> None:
        scan = AsyncMock(
            side_effect=[
                OperationalError("due scan", {}, Exception("simulated")),
                None,
                None,
            ]
        )
        discovery = AsyncMock(return_value=0)
        controlled_sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])

        with (
            patch.object(self.scheduler, "process_due_deliveries", scan),
            patch.object(self.scheduler, "discover_events", discovery),
            patch.object(self.scheduler, "sleep", controlled_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.scheduler.run()

        self.assertEqual(scan.await_count, 3)
        self.assertEqual(discovery.await_count, 2)

    async def test_post_discovery_scan_failure_does_not_stop_scheduler(self) -> None:
        scan = AsyncMock(
            side_effect=[
                None,
                OperationalError("due scan", {}, Exception("simulated")),
                None,
            ]
        )
        discovery = AsyncMock(return_value=0)
        controlled_sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])

        with (
            patch.object(self.scheduler, "process_due_deliveries", scan),
            patch.object(self.scheduler, "discover_events", discovery),
            patch.object(self.scheduler, "sleep", controlled_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.scheduler.run()

        self.assertEqual(scan.await_count, 3)
        self.assertEqual(discovery.await_count, 2)

    async def test_discovery_failure_does_not_prevent_later_iteration(self) -> None:
        scan = AsyncMock(return_value=0)
        discovery = AsyncMock(side_effect=[RuntimeError("bad payload"), 0])
        controlled_sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])

        with (
            patch.object(self.scheduler, "process_due_deliveries", scan),
            patch.object(self.scheduler, "discover_events", discovery),
            patch.object(self.scheduler, "sleep", controlled_sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.scheduler.run()

        self.assertEqual(discovery.await_count, 2)
        self.assertEqual(scan.await_count, 3)

    async def test_run_propagates_cancellation_from_active_phase_promptly(self) -> None:
        started = asyncio.Event()

        async def discovery() -> int:
            started.set()
            await asyncio.Event().wait()
            return 0

        with (
            patch.object(self.scheduler, "process_due_deliveries", AsyncMock(return_value=0)),
            patch.object(self.scheduler, "discover_events", side_effect=discovery),
        ):
            task = asyncio.create_task(self.scheduler.run())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.2)

    async def test_already_sent_delivery_is_not_resent(self) -> None:
        await self.seed_delivery(status=DeliveryStatus.SENT.value)

        self.assertEqual(await self.scheduler.process_due_deliveries(), 0)
        self.bot.send_message.assert_not_awaited()

    async def test_crash_after_telegram_success_before_commit_can_duplicate(self) -> None:
        """Document the unavoidable accepted-by-Telegram/pre-commit duplicate window."""

        await self.seed_delivery()
        with (
            patch.object(
                self.scheduler,
                "_record_success",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.scheduler.process_due_deliveries()

        self.assertEqual((await self.deliveries())[0].status, DeliveryStatus.PENDING.value)
        restarted_bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
        restarted = ScheduleScheduler(
            restarted_bot,
            service=self.service,
            session_factory=self.session,
            clock=lambda: self.now,
        )
        await restarted.process_due_deliveries()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(restarted_bot.send_message.await_count, 1)

    async def test_no_database_transaction_is_open_during_telegram_io(self) -> None:
        await self.seed_delivery()
        opened_sessions = []

        def tracked_session():
            session = self.session()
            opened_sessions.append(session)
            return session

        scheduler = ScheduleScheduler(
            self.bot,
            service=self.service,
            session_factory=tracked_session,  # type: ignore[arg-type]
            clock=lambda: self.now,
        )

        async def send(*_args: object, **_kwargs: object) -> object:
            self.assertTrue(opened_sessions)
            self.assertTrue(all(not session.in_transaction() for session in opened_sessions))
            return object()

        self.bot.send_message.side_effect = send

        await scheduler.process_due_deliveries()

    async def test_overlapping_delivery_loops_do_not_deliberately_send_twice(self) -> None:
        await self.seed_delivery()
        release = asyncio.Event()

        async def send(*_args: object, **_kwargs: object) -> object:
            release.set()
            await asyncio.sleep(0)
            return object()

        self.bot.send_message.side_effect = send
        first = asyncio.create_task(self.scheduler.process_due_deliveries())
        await release.wait()
        second = asyncio.create_task(self.scheduler.process_due_deliveries())
        await asyncio.gather(first, second)

        self.assertEqual(self.bot.send_message.await_count, 1)
