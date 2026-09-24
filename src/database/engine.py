from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

engine = create_async_engine(settings.DATABASE_URL, echo=False)


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def configure_sqlite_foreign_keys(target_engine: AsyncEngine) -> None:
    event.listen(target_engine.sync_engine, "connect", _enable_sqlite_foreign_keys)


configure_sqlite_foreign_keys(engine)

async_session = async_sessionmaker(engine, expire_on_commit=False)
