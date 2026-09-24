"""Add durable schedule-event notification outbox.

Revision ID: f412bdd7c3e0
Revises: initial_schema
Create Date: 2026-09-22 14:47:18.701643

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f412bdd7c3e0"
down_revision: str | Sequence[str] | None = "initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_schedule_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("date_graph", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_group", sa.String(), nullable=False),
        sa.Column("event_date", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'exhausted', 'terminal')",
            name="ck_notification_deliveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_notification_deliveries_attempt_count"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["processed_schedule_events.event_id"],
            name="fk_notification_deliveries_event_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "chat_id", name="uq_notification_deliveries_event_chat"
        ),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )

    op.drop_table("schedule_state")


def downgrade() -> None:
    op.create_table(
        "schedule_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_table("processed_schedule_events")
