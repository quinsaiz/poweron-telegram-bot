from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import settings
from src.database.models import Base

engine = create_async_engine(settings.DATABASE_URL, echo=False)

async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:  # noqa
        await conn.run_sync(Base.metadata.create_all)
