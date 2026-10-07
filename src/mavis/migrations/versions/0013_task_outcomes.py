"""hotfix4 H1: truthful task outcomes.

- tasks.status may now be 'partial' (a plain string column, no constraint to change).
- tasks.turn_ref: the chat turn that produced the task (APPROVAL tasks link their approvals to it).
- tasks.acknowledged_at, pending_approvals.acknowledged_at: a failed/partial outcome stays in "recently
  failed" until the user acknowledges it.
- pending_approvals.failure_reason: the user-safe reason an approved action failed.

Existing rows: no turn, not acknowledged, no reason (they read as "it did not go through").
NOTE: Phase B (branch `ledger`) also chains a 0013 revision (0013_commitments) after 0012; whichever
merges second must renumber its revision and down_revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_task_outcomes"
down_revision = "0012_loop_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("turn_ref", sa.String(160), nullable=True))
        batch.add_column(sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("pending_approvals") as batch:
        batch.add_column(sa.Column("failure_reason", sa.String(300), nullable=True))
        batch.add_column(sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pending_approvals") as batch:
        batch.drop_column("acknowledged_at")
        batch.drop_column("failure_reason")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("acknowledged_at")
        batch.drop_column("turn_ref")
