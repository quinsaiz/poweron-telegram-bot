"""Track the schedule snapshot last seen for each upstream event.

poweron updates an existing ActualGpvGraph row in place when a schedule is
revised. The snapshot hash lets the outbox detect such revisions even though
the upstream event id did not change.

Revision ID: 20261120_02_event_snapshot_hash
Revises: 20260925_initial_schema
"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_02_event_snapshot_hash"
down_revision = "20260925_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processed_schedule_events",
        sa.Column("snapshot_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("processed_schedule_events", "snapshot_hash")
