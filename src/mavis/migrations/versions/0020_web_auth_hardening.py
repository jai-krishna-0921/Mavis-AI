"""Web sign-in hardening: approval details on login nonces, server-side pending email links, and the
dashboard session a web connect was started from."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_web_auth_hardening"
down_revision = "0019_web_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("web_login_nonces") as b:
        b.add_column(sa.Column("code", sa.String(4), nullable=False, server_default=""))
        b.add_column(sa.Column("agent_hint", sa.String(80), nullable=False, server_default=""))
        b.add_column(sa.Column("place_hint", sa.String(64), nullable=False, server_default=""))
        b.add_column(sa.Column("pending_user_id", sa.Integer, sa.ForeignKey(
            "users.id", name="fk_web_login_nonces_pending_user_id_users"), nullable=True))
    op.add_column("native_oauth_states", sa.Column("session_hash", sa.String(64), nullable=True))
    op.create_table(
        "web_pending_links",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("pre_hash", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_web_pending_links_expires_at", "web_pending_links", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_web_pending_links_expires_at", table_name="web_pending_links")
    op.drop_table("web_pending_links")
    op.drop_column("native_oauth_states", "session_hash")
    with op.batch_alter_table("web_login_nonces") as b:
        b.drop_constraint("fk_web_login_nonces_pending_user_id_users", type_="foreignkey")
        for col in ("pending_user_id", "place_hint", "agent_hint", "code"):
            b.drop_column(col)
