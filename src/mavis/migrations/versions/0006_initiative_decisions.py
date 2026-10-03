"""initiative: persisted reasoner decisions (retry reuses them)"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_initiative_decisions"
down_revision = "0005_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "initiative_decisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("event_id", sa.String(200), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decision", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_initiative_decisions_user_id"), "initiative_decisions", ["user_id"])
    op.create_index(op.f("ix_initiative_decisions_created_at"), "initiative_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_table("initiative_decisions")
