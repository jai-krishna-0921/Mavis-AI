"""integrations: connections_pending"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_integrations"
down_revision = "0003_initiative"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connections_pending",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("capability", sa.String(40), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_connections_pending_user_id"), "connections_pending", ["user_id"])
    op.create_index(op.f("ix_connections_pending_capability"), "connections_pending", ["capability"])
    op.create_index(op.f("ix_connections_pending_status"), "connections_pending", ["status"])


def downgrade() -> None:
    op.drop_table("connections_pending")
