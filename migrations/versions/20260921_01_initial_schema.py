"""Create the application schema.

Revision ID: initial_schema
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("group", sa.String(), nullable=False),
        sa.UniqueConstraint("chat_id"),
    )
    op.create_table(
        "schedule_state",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("last_id", sa.Integer(), nullable=False),
    )
    op.create_table(
        "schedule_cache",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("date_graph", sa.String(), nullable=False),
        sa.Column("group", sa.String(), nullable=False),
        sa.Column("times_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("date_graph", "group", name="uq_schedule_cache_date_group"),
    )
    op.create_table(
        "banned_users",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("until_date", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("banned_users")
    op.drop_table("schedule_cache")
    op.drop_table("schedule_state")
    op.drop_table("users")
