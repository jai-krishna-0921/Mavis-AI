"""Phase 12 slice A: progress cards.

- task_cards: the live status message per user task (state survives a resume).
- outbox.photo_path, outbox.media: photos and albums go through the outbox (durable, deduped).
- artifacts.delivered_at: files are sent as soon as they exist; delivery skips sent ones.

NOTE: parallel branches (ledger, multi-user, programs, connectors) also chain revisions after
0013_task_outcomes; whichever merges later renumbers its revision and down_revision
(tests/store/test_migrations.py::test_single_migration_head guards it).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_progress_cards"
down_revision = "0013_task_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_cards",
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=True),
        sa.Column("state", sa.JSON, nullable=False),
        sa.Column("last_edit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_cards_user_id", "task_cards", ["user_id"])
    with op.batch_alter_table("outbox") as batch:
        batch.add_column(sa.Column("photo_path", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("media", sa.JSON, nullable=True))
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_column("delivered_at")
    with op.batch_alter_table("outbox") as batch:
        batch.drop_column("media")
        batch.drop_column("photo_path")
    op.drop_index("ix_task_cards_user_id", table_name="task_cards")
    op.drop_table("task_cards")
