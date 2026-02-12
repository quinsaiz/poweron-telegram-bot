from sqlalchemy import BigInteger, String, Integer, Text, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime, timezone


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

    id: Mapped[int] = mapped_column(primary_key=True)
    date_graph: Mapped[str] = mapped_column(String, unique=True)
    group: Mapped[str] = mapped_column(String, default="3.2")
    times_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class BannedUser(Base):
    __tablename__ = "banned_users"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    until_date: Mapped[datetime] = mapped_column(DateTime)
