"""Add durable PowerOn source and shared-group state.

Revision ID: poweron_source_state
Revises: f412bdd7c3e0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "poweron_source_state"
down_revision: str | Sequence[str] | None = "f412bdd7c3e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("group")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "group",
                sa.String(),
                nullable=False,
                server_default=sa.text("'3.2'"),
            )
        )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("group", server_default=None)
    op.drop_table("poweron_source_state")
