"""Native Google and Slack connectors: sealed per-user grants and single-use OAuth states.

NOTE: parked branches (connectors, ledger, programs, multi-user) renumber after this ships
(tests/store/test_migrations.py::test_single_migration_head guards it).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_native_grants"
down_revision = "0014_progress_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_grants",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("account", sa.JSON, nullable=False),
        sa.Column("access_token", sa.Text, nullable=False),
        sa.Column("refresh_token", sa.Text, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_native_grants_user_provider"),
    )
    op.create_index("ix_native_grants_user_id", "native_grants", ["user_id"])
    op.create_table(
        "native_oauth_states",
        sa.Column("nonce", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("pending_id", sa.Integer, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_native_oauth_states_user_id", "native_oauth_states", ["user_id"])
    op.create_index("ix_native_oauth_states_expires_at", "native_oauth_states", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_native_oauth_states_expires_at", table_name="native_oauth_states")
    op.drop_index("ix_native_oauth_states_user_id", table_name="native_oauth_states")
    op.drop_table("native_oauth_states")
    op.drop_index("ix_native_grants_user_id", table_name="native_grants")
    op.drop_table("native_grants")
