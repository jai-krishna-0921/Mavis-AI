"""Machine sessions, compute usage, workspace files and user quotas (Phase 12 slice B).

NOTE for the merge: this revision is named 0021 and points at 0016_outbox_route, main's head when it was
written. Other branches take 0017 (multiuser), 0018 (personal layer), 0019 (web sessions) and possibly 0020
(chat clear). Re-point down_revision at the merged head right before merging."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_machine"
down_revision = "0021_chat_message_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "machine_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("backend", sa.String(24), nullable=False),
        sa.Column("session_id", sa.String(200), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wall_s", sa.Float, nullable=False, server_default="0"),
        sa.Column("est_cost_usd", sa.Float, nullable=False, server_default="0"),
    )
    op.create_index("ix_machine_sessions_user_id", "machine_sessions", ["user_id"])
    op.create_index("ix_machine_sessions_task_id", "machine_sessions", ["task_id"])
    op.create_index("ix_machine_sessions_status", "machine_sessions", ["status"])
    op.create_table(
        "compute_usage",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("task_id", sa.Integer, nullable=True),
        sa.Column("session_id", sa.String(200), nullable=False),
        sa.Column("wall_s", sa.Float, nullable=False, server_default="0"),
        sa.Column("est_cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_compute_usage_session"),
    )
    op.create_index("ix_compute_usage_user_id", "compute_usage", ["user_id"])
    op.create_table(
        "workspace_files",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("cls", sa.String(8), nullable=False),
        sa.Column("provenance", sa.String(24), nullable=False),
        sa.Column("task_id", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "path", name="uq_workspace_files_user_path"),
    )
    op.create_index("ix_workspace_files_user_id", "workspace_files", ["user_id"])
    op.create_table(
        "user_quotas",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_user_quotas_user_key"),
    )
    op.create_index("ix_user_quotas_user_id", "user_quotas", ["user_id"])


def downgrade() -> None:
    for table, index in (("user_quotas", "ix_user_quotas_user_id"),
                         ("workspace_files", "ix_workspace_files_user_id"),
                         ("compute_usage", "ix_compute_usage_user_id")):
        op.drop_index(index, table_name=table)
        op.drop_table(table)
    for index in ("ix_machine_sessions_status", "ix_machine_sessions_task_id", "ix_machine_sessions_user_id"):
        op.drop_index(index, table_name="machine_sessions")
    op.drop_table("machine_sessions")
