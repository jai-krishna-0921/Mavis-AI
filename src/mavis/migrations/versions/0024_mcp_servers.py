# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""MCP client (v2 track 1): the servers a user connected and the consent redirects in flight."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_mcp_servers"
down_revision = "0023_outbox_dedupe_per_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("slug", sa.String(24), nullable=False),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("catalog_id", sa.String(40), nullable=True),
        sa.Column("auth_kind", sa.String(8), nullable=False),
        sa.Column("access_token", sa.Text, nullable=True),
        sa.Column("refresh_token", sa.Text, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oauth", sa.JSON, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("last_error", sa.String(32), nullable=True),
        sa.Column("tools", sa.JSON, nullable=False),
        sa.Column("tools_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "slug", name="uq_mcp_servers_user_slug"),
    )
    op.create_index("ix_mcp_servers_user_id", "mcp_servers", ["user_id"])
    op.create_table(
        "mcp_oauth_states",
        sa.Column("state", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("server_id", sa.Integer, sa.ForeignKey("mcp_servers.id"), nullable=False),
        sa.Column("code_verifier", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_oauth_states_user_id", "mcp_oauth_states", ["user_id"])
    op.create_index("ix_mcp_oauth_states_server_id", "mcp_oauth_states", ["server_id"])
    op.create_index("ix_mcp_oauth_states_expires_at", "mcp_oauth_states", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_mcp_oauth_states_expires_at", table_name="mcp_oauth_states")
    op.drop_index("ix_mcp_oauth_states_server_id", table_name="mcp_oauth_states")
    op.drop_index("ix_mcp_oauth_states_user_id", table_name="mcp_oauth_states")
    op.drop_table("mcp_oauth_states")
    op.drop_index("ix_mcp_servers_user_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
