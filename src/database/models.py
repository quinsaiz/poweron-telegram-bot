from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    group: Mapped[str] = mapped_column(String, default="3.2")


class ScheduleState(Base):
    __tablename__ = "schedule_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_id: Mapped[int] = mapped_column(Integer)


class ScheduleCache(Base):
    __tablename__ = "schedule_cache"
    __table_args__ = (UniqueConstraint("date_graph", "group", name="uq_schedule_cache_date_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date_graph: Mapped[str] = mapped_column(String)
    group: Mapped[str] = mapped_column(String, default="3.2")
    times_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class BannedUser(Base):
    __tablename__ = "banned_users"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    until_date: Mapped[datetime] = mapped_column(DateTime)
