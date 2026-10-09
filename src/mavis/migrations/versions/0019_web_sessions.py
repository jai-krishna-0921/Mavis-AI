"""Web dashboard: sessions, Telegram link sign-in nonces, confirmed emails, and where a native OAuth
consent was started from (so a web connect returns to the dashboard)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_web_sessions"
down_revision = "0018_personal_layer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("native_oauth_states", sa.Column("origin", sa.String(8), nullable=True))
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(200), nullable=False, server_default=""),
    )
    op.create_index("ix_web_sessions_user_id", "web_sessions", ["user_id"])
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])
    op.create_table(
        "web_login_nonces",
        sa.Column("nonce_hash", sa.String(64), primary_key=True),
        sa.Column("pre_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("link_email", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_web_login_nonces_user_id", "web_login_nonces", ["user_id"])
    op.create_index("ix_web_login_nonces_expires_at", "web_login_nonces", ["expires_at"])
    op.create_table(
        "user_emails",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_emails_user_id", "user_emails", ["user_id"])


def downgrade() -> None:
    op.drop_column("native_oauth_states", "origin")
    op.drop_index("ix_user_emails_user_id", table_name="user_emails")
    op.drop_table("user_emails")
    op.drop_index("ix_web_login_nonces_expires_at", table_name="web_login_nonces")
    op.drop_index("ix_web_login_nonces_user_id", table_name="web_login_nonces")
    op.drop_table("web_login_nonces")
    op.drop_index("ix_web_sessions_expires_at", table_name="web_sessions")
    op.drop_index("ix_web_sessions_user_id", table_name="web_sessions")
    op.drop_table("web_sessions")
