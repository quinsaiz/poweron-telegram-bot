from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from src.config import settings
from src.database.engine import async_session
from src.database.models import PowerOnSourceState
from src.database.source_state import source_state_write_transaction
from src.domain_time import utc_now
from src.logger import setup_logger
from src.poweron.schemas import BuildingGroupsResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = setup_logger(__name__, settings.LOG_LEVEL)

SOURCE_STATE_ID = 1
SUCCESS_TTL = timedelta(hours=1)
FAILURE_RETRY_DELAY = timedelta(minutes=10)
GROUP_REQUEST_TIMEOUT_SECONDS = 10.0


class SourceIdentityError(RuntimeError):
    pass


class GroupDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    group: str | None
    last_refresh_attempt_at: datetime | None
    last_successful_refresh_at: datetime | None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class PowerOnGroupResolver:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        clock: Callable[[], datetime] = utc_now,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.session_factory = session_factory or async_session
        self.clock = clock
        self.transport = transport
        self._lock = asyncio.Lock()
        self._group_url = f"{settings.POWERON_API_URL}/pw-accounts/building-groups"

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Group resolver clock must return an aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _city_mismatch_error(stored_city_id: int) -> SourceIdentityError:
        return SourceIdentityError(
            "Configured POWERON_CITY_ID does not match this database "
            f"(configured={settings.POWERON_CITY_ID}, stored={stored_city_id}). "
            "Stop the service, remove and recreate the disposable database manually, "
            "then restart with the new city. The application did not modify the city ID."
        )

    async def _load_or_create_source(self) -> SourceSnapshot:
        async with source_state_write_transaction(self.session_factory) as session:
            state = await session.get(PowerOnSourceState, SOURCE_STATE_ID)
            if state is None:
                state = PowerOnSourceState(
                    id=SOURCE_STATE_ID,
                    city_id=settings.POWERON_CITY_ID,
                    group=None,
                    last_refresh_attempt_at=None,
                    last_successful_refresh_at=None,
                )
                session.add(state)
                await session.flush()
            elif state.city_id != settings.POWERON_CITY_ID:
                raise self._city_mismatch_error(state.city_id)

            return SourceSnapshot(
                group=state.group,
                last_refresh_attempt_at=_as_utc(state.last_refresh_attempt_at),
                last_successful_refresh_at=_as_utc(state.last_successful_refresh_at),
            )

    async def ensure_source_identity(self) -> None:
        async with self._lock:
            await self._load_or_create_source()

    @staticmethod
    def _refresh_due(snapshot: SourceSnapshot, now: datetime) -> bool:
        attempt = snapshot.last_refresh_attempt_at
        success = snapshot.last_successful_refresh_at
        if success is not None and (attempt is None or success >= attempt):
            return now - success >= SUCCESS_TTL
        if attempt is not None:
            return now - attempt >= FAILURE_RETRY_DELAY
        return True

    async def _fetch_group(self) -> str:
        city_id_base64 = base64.b64encode(str(settings.POWERON_CITY_ID).encode()).decode()
        async with httpx.AsyncClient(
            headers={
                "Accept": "application/json",
                "X-debug-key": city_id_base64,
            },
            follow_redirects=True,
            timeout=GROUP_REQUEST_TIMEOUT_SECONDS,
            transport=self.transport,
        ) as client:
            response = await client.get(
                self._group_url,
                params={"cityId": settings.POWERON_CITY_ID},
            )
        response.raise_for_status()

        media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise GroupDiscoveryError("group endpoint returned a non-JSON content type")

        payload = BuildingGroupsResponse.model_validate(response.json())
        group = payload.authoritative_group()
        if group is None:
            count = len(payload.normalized_groups())
            raise GroupDiscoveryError(
                f"group endpoint did not return exactly one normalized group (count={count})"
            )
        return group

    async def _record_failure(self, attempted_at: datetime) -> None:
        async with source_state_write_transaction(self.session_factory) as session:
            state = await session.get(PowerOnSourceState, SOURCE_STATE_ID)
            if state is None:
                raise RuntimeError("poweron source state disappeared during group refresh")
            if state.city_id != settings.POWERON_CITY_ID:
                raise self._city_mismatch_error(state.city_id)
            state.last_refresh_attempt_at = attempted_at

    async def _record_success(self, group: str, refreshed_at: datetime) -> str:
        old_group: str | None
        async with source_state_write_transaction(self.session_factory) as session:
            state = await session.get(PowerOnSourceState, SOURCE_STATE_ID)
            if state is None:
                raise RuntimeError("poweron source state disappeared during group refresh")
            if state.city_id != settings.POWERON_CITY_ID:
                raise self._city_mismatch_error(state.city_id)
            old_group = state.group
            state.group = group
            state.last_refresh_attempt_at = refreshed_at
            state.last_successful_refresh_at = refreshed_at

        if old_group is not None and old_group != group:
            logger.info("poweron group changed from %s to %s", old_group, group)
        return group

    async def ensure_group(self) -> str | None:
        async with self._lock:
            snapshot = await self._load_or_create_source()
            attempted_at = self._now()
            if not self._refresh_due(snapshot, attempted_at):
                return snapshot.group

            try:
                group = await self._fetch_group()
            except asyncio.CancelledError:
                raise
            except (GroupDiscoveryError, ValidationError, ValueError, httpx.HTTPError) as error:
                await self._record_failure(attempted_at)
                logger.warning("poweron group refresh failed: %s", type(error).__name__)
                return snapshot.group

            return await self._record_success(group, attempted_at)


group_resolver = PowerOnGroupResolver()
