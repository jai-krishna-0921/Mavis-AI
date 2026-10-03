"""hotfix3: pending_approvals.tainted (approval dedupe never merges tainted and untainted requests)

Rows queued before this revision read as untainted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_hotfix_approval_taint"
down_revision = "0009_orchestrator_followups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pending_approvals") as batch:
        batch.add_column(sa.Column("tainted", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("pending_approvals") as batch:
        batch.drop_column("tainted")
