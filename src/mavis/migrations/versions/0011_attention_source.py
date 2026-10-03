"""attention_observations.source: mail or a Google Workspace signal source (Workspace spec section 6)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_attention_source"
down_revision = "0010_hotfix_approval_taint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("attention_observations") as batch:
        batch.add_column(sa.Column("source", sa.String(12), nullable=False, server_default="mail"))
    op.create_index("ix_attention_observations_user_source", "attention_observations", ["user_id", "source"])


def downgrade() -> None:
    op.drop_index("ix_attention_observations_user_source", table_name="attention_observations")
    with op.batch_alter_table("attention_observations") as batch:
        batch.drop_column("source")
