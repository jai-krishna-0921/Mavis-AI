"""Personal layer per user: versioned layer documents, learning suppressions and deterministic signals.

NOTE: down_revision skips 0017, which the multi-user merge takes; the merge re-points this revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_personal_layer"
down_revision = "0017_multiuser_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_layers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "version", name="uq_personal_layers_user_version"),
    )
    op.create_index("ix_personal_layers_user_id", "personal_layers", ["user_id"])
    op.create_table(
        "learning_suppressions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.String(240), nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_learning_suppressions_user_key"),
    )
    op.create_index("ix_learning_suppressions_user_id", "learning_suppressions", ["user_id"])
    op.create_table(
        "personal_signals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("source_ref", sa.String(200), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meta", sa.JSON, nullable=False),
        sa.UniqueConstraint("user_id", "kind", "key", "source_ref", name="uq_personal_signals_identity"),
    )
    op.create_index("ix_personal_signals_user_id", "personal_signals", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_personal_signals_user_id", table_name="personal_signals")
    op.drop_table("personal_signals")
    op.drop_index("ix_learning_suppressions_user_id", table_name="learning_suppressions")
    op.drop_table("learning_suppressions")
    op.drop_index("ix_personal_layers_user_id", table_name="personal_layers")
    op.drop_table("personal_layers")
