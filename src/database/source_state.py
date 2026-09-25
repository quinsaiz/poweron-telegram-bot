"""Atomic SQLite transactions for the singleton poweron source identity."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy import text

from src.database.models import PowerOnSourceState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_source_write_lock = asyncio.Lock()


@asynccontextmanager
async def source_state_write_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Serialize source-state writers and acquire SQLite's write lock before reading."""

    async with _source_write_lock, session_factory() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def source_authority_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    city_id: int,
    group: str,
) -> AsyncIterator[AsyncSession | None]:
    """Yield a write transaction only while the expected source remains authoritative."""

    async with source_state_write_transaction(session_factory) as session:
        state = await session.get(PowerOnSourceState, 1)
        if state is None or state.city_id != city_id or state.group != group:
            yield None
        else:
            yield session
