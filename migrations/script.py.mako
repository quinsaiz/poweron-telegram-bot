"""${message}

Revision ID: ${up_revision}
Revises:${" " if down_revision else ""}${down_revision | comma,n}
Create Date: ${create_date}

"""

from __future__ import annotations

from typing import TYPE_CHECKING
% if "sa." in (upgrades or "") or "sa." in (downgrades or ""):

import sqlalchemy as sa
% endif
% if "op." in (upgrades or "") or "op." in (downgrades or ""):
from alembic import op
% endif
% if imports:
${imports}
% endif

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = ${repr(up_revision).replace("'", '"')}
down_revision: str | Sequence[str] | None = ${repr(down_revision).replace("'", '"')}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels).replace("'", '"')}
depends_on: str | Sequence[str] | None = ${repr(depends_on).replace("'", '"')}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
