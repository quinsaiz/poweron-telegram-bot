from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)


class PowerOnSourceState(Base):
    __tablename__ = "poweron_source_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_poweron_source_state_singleton"),
        CheckConstraint("city_id > 0", name="ck_poweron_source_state_city_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(Integer)
    group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_refresh_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_successful_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ScheduleCache(Base):
    __tablename__ = "schedule_cache"
    __table_args__ = (UniqueConstraint("date_graph", "group", name="uq_schedule_cache_date_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date_graph: Mapped[str] = mapped_column(String)
    group: Mapped[str] = mapped_column(String)
    times_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class BannedUser(Base):
    __tablename__ = "banned_users"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    until_date: Mapped[datetime] = mapped_column(DateTime)


class ProcessedScheduleEvent(Base):
    __tablename__ = "processed_schedule_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    date_graph: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'exhausted', 'terminal')",
            name="ck_notification_deliveries_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_notification_deliveries_attempt_count"),
        UniqueConstraint("event_id", "chat_id", name="uq_notification_deliveries_event_chat"),
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(
        ForeignKey(
            "processed_schedule_events.event_id",
            name="fk_notification_deliveries_event_id",
            ondelete="RESTRICT",
        )
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    recipient_group: Mapped[str] = mapped_column(String)
    event_date: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)
    attempt_count: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
