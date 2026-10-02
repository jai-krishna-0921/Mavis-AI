"""initiative engine: loops, wakeups, ping_log"""

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
        sa.Column("version", sa.Integer, nullable=False),
    )
    op.create_index("ix_loops_user_id", "loops", ["user_id"])
    op.create_index("ix_loops_due_at", "loops", ["due_at"])
    op.create_index("ix_loops_status", "loops", ["status"])
    op.create_table(
        "wakeups",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("loop_id", sa.Integer, nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wakeups_user_id", "wakeups", ["user_id"])
    op.create_index("ix_wakeups_loop_id", "wakeups", ["loop_id"])
    op.create_index("ix_wakeups_dedupe_key", "wakeups", ["dedupe_key"])
    op.create_index("ix_wakeups_status_due", "wakeups", ["status", "due_at"])
    op.create_index(
        "uq_wakeups_pending_dedupe",
        "wakeups",
        ["user_id", "dedupe_key"],
        unique=True,
        sqlite_where=sa.text("status = 'pending' AND dedupe_key IS NOT NULL"),
        postgresql_where=sa.text("status = 'pending' AND dedupe_key IS NOT NULL"),
    )
    op.create_table(
        "ping_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.String(240), nullable=False),
        sa.Column("urgency", sa.Integer, nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_ping_log_user_key"),
    )
    op.create_index("ix_ping_log_user_id", "ping_log", ["user_id"])


def downgrade() -> None:
    op.drop_table("ping_log")
    op.drop_table("wakeups")
    op.drop_index("ix_loops_status", table_name="loops")
    op.drop_index("ix_loops_due_at", table_name="loops")
    op.drop_index("ix_loops_user_id", table_name="loops")
    op.drop_table("loops")
