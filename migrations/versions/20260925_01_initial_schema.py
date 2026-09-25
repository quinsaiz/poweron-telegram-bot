"""Create the complete application schema.

Revision ID: 20260925_initial_schema
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "20260925_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id"),
    )
    op.create_table(
        "poweron_source_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("group", sa.String(length=32), nullable=True),
        sa.Column("last_refresh_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("city_id > 0", name="ck_poweron_source_state_city_positive"),
        sa.CheckConstraint("id = 1", name="ck_poweron_source_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "schedule_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date_graph", sa.String(), nullable=False),
        sa.Column("group", sa.String(), nullable=False),
        sa.Column("times_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date_graph", "group", name="uq_schedule_cache_date_group"),
    )
    op.create_table(
        "banned_users",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("until_date", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id"),
    )
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
        sa.CheckConstraint("attempt_count >= 0", name="ck_notification_deliveries_attempt_count"),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'exhausted', 'terminal')",
            name="ck_notification_deliveries_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["processed_schedule_events.event_id"],
            name="fk_notification_deliveries_event_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "chat_id", name="uq_notification_deliveries_event_chat"),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_table("processed_schedule_events")
    op.drop_table("banned_users")
    op.drop_table("schedule_cache")
    op.drop_table("poweron_source_state")
    op.drop_table("users")
