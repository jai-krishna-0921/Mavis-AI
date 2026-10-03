"""orchestrator follow-ups: pending_approvals.resolving_at, tasks.source_ref (unique per user)

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
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("source_ref", sa.String(length=160), nullable=True))
        batch.create_unique_constraint("uq_tasks_user_source_ref", ["user_id", "source_ref"])


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("uq_tasks_user_source_ref", type_="unique")
        batch.drop_column("source_ref")
    with op.batch_alter_table("pending_approvals") as batch:
        batch.drop_column("resolving_at")
