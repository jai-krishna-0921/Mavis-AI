"""initiative engine: loops (wakeups and ping_log added in later tasks)"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_initiative"
down_revision = "0002_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loops",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entities", sa.JSON, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("importance", sa.Integer, nullable=False),
        sa.Column("watch", sa.JSON, nullable=True),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_loops_user_id", "loops", ["user_id"])
    op.create_index("ix_loops_due_at", "loops", ["due_at"])
    op.create_index("ix_loops_status", "loops", ["status"])


def downgrade() -> None:
    op.drop_index("ix_loops_status", table_name="loops")
    op.drop_index("ix_loops_due_at", table_name="loops")
    op.drop_index("ix_loops_user_id", table_name="loops")
    op.drop_table("loops")
