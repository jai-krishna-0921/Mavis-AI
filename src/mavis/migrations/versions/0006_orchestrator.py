"""orchestrator: tasks, artifacts, pending_approvals, policy_rules, audit_log"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_orchestrator"
down_revision = "0005_integrations"
branch_labels = None
depends_on = None

_DT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("parent_id", sa.Integer, nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("context", sa.Text, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("notify_on_complete", sa.Boolean, nullable=False),
        sa.Column("plan", sa.JSON, nullable=True),
        sa.Column("result_text", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", _DT, nullable=False),
        sa.Column("started_at", _DT, nullable=True),
        sa.Column("finished_at", _DT, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["tasks.id"]),
    )
    op.create_index(op.f("ix_tasks_user_id"), "tasks", ["user_id"])
    op.create_index(op.f("ix_tasks_status"), "tasks", ["status"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_id", sa.Integer, nullable=False),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("mime", sa.String(120), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("created_at", _DT, nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_artifacts_task_id"), "artifacts", ["task_id"])
    op.create_index(op.f("ix_artifacts_user_id"), "artifacts", ["user_id"])

    op.create_table(
        "pending_approvals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("task_id", sa.Integer, nullable=True),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("arguments", sa.JSON, nullable=False),
        sa.Column("preview", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.Text, nullable=True),
        sa.Column("created_at", _DT, nullable=False),
        sa.Column("expires_at", _DT, nullable=False),
        sa.Column("prompted_at", _DT, nullable=True),
        sa.Column("resolved_at", _DT, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
    )
    op.create_index(op.f("ix_pending_approvals_user_id"), "pending_approvals", ["user_id"])
    op.create_index(op.f("ix_pending_approvals_task_id"), "pending_approvals", ["task_id"])
    op.create_index(op.f("ix_pending_approvals_status"), "pending_approvals", ["status"])

    op.create_table(
        "policy_rules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("contains", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("created_at", _DT, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_policy_rules_user_id"), "policy_rules", ["user_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("created_at", _DT, nullable=False),
    )
    op.create_index(op.f("ix_audit_log_user_id"), "audit_log", ["user_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("policy_rules")
    op.drop_table("pending_approvals")
    op.drop_table("artifacts")
    op.drop_table("tasks")
