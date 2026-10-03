"""orchestrator follow-ups: pending_approvals.resolving_at

0007_orchestrator is frozen (chat-tools deploys it first), so later orchestrator columns land here.
down_revision is 0007_orchestrator; if 0008_attention merges first, re-point it to 0008_attention.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_orchestrator_followups"
down_revision = "0007_orchestrator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pending_approvals") as batch:
        batch.add_column(sa.Column("resolving_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pending_approvals") as batch:
        batch.drop_column("resolving_at")
