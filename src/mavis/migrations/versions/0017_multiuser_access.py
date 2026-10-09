"""Phase 11 multi-user: user status/tier/identity columns, invite codes, llm usage, outbox priority.

Existing users become active (they were admitted by the allowlist); a user whose chat is in
OWNER_TELEGRAM_CHAT_IDS (or the old ALLOWED_TELEGRAM_CHAT_IDS) becomes tier owner. composio_user_id stays
NULL for them, which means the legacy `mavis-<id>` identity (spec 6.2), so connected accounts keep working.
"""

from __future__ import annotations

import json
import os

import sqlalchemy as sa
from alembic import op

revision = "0017_multiuser_access"
down_revision = "0016_outbox_route"
branch_labels = None
depends_on = None


def _owner_ids() -> list[int]:
    raw = os.environ.get("OWNER_TELEGRAM_CHAT_IDS") or os.environ.get("ALLOWED_TELEGRAM_CHAT_IDS") or "[]"
    try:
        return [int(x) for x in json.loads(raw)]
    except (ValueError, TypeError):
        return [int(x) for x in raw.strip("[]").split(",") if x.strip()]


def upgrade() -> None:
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("code_hint", sa.String(8), nullable=False),
        sa.Column("label", sa.String(120), nullable=False, server_default=""),
        sa.Column("tier", sa.String(16), nullable=False, server_default="standard"),
        sa.Column("max_uses", sa.Integer, nullable=False, server_default="1"),
        sa.Column("uses", sa.Integer, nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Integer, nullable=True),
        sa.Column("default_timezone", sa.String(64), nullable=True),
        sa.Column("default_currency", sa.String(3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "invite_redemptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("invite_id", sa.Integer, sa.ForeignKey("invite_codes.id"), nullable=False),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_invite_redemptions_invite_id", "invite_redemptions", ["invite_id"])
    op.create_index("ix_invite_redemptions_user_id", "invite_redemptions", ["user_id"])
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cost_micros", sa.BigInteger, nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "day", "provider", "model", "purpose",
                            name="uq_llm_usage_user_day_model_purpose"),
    )
    op.create_index("ix_llm_usage_user_id", "llm_usage", ["user_id"])
    op.create_index("ix_llm_usage_day", "llm_usage", ["day"])
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("status", sa.String(16), nullable=False, server_default="pending"))
        b.add_column(sa.Column("tier", sa.String(16), nullable=False, server_default="standard"))
        b.add_column(sa.Column("telegram_user_id", sa.BigInteger, nullable=True))
        b.add_column(sa.Column("locale", sa.String(16), nullable=True))
        b.add_column(sa.Column("currency", sa.String(3), nullable=True))
        b.add_column(sa.Column("country", sa.String(2), nullable=True))
        b.add_column(sa.Column("composio_user_id", sa.String(64), nullable=True))
        b.add_column(sa.Column("invite_id", sa.Integer, nullable=True))
        b.add_column(sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("ban_reason", sa.String(200), nullable=True))
        b.add_column(sa.Column("budget_override_usd_day", sa.Float, nullable=True))
        b.add_column(sa.Column("inactive_since", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("is_test", sa.Boolean, nullable=False, server_default=sa.false()))
        b.create_index("ix_users_status", ["status"])
        b.create_unique_constraint("uq_users_composio_user_id", ["composio_user_id"])
        b.create_foreign_key("fk_users_invite_id", "invite_codes", ["invite_id"], ["id"])
    with op.batch_alter_table("outbox") as b:
        b.add_column(sa.Column("priority", sa.Integer, nullable=False, server_default="0"))
        b.create_index("ix_outbox_priority", ["priority"])
    users = sa.table("users", sa.column("telegram_chat_id", sa.BigInteger), sa.column("status", sa.String),
                     sa.column("tier", sa.String), sa.column("telegram_user_id", sa.BigInteger))
    op.execute(users.update().values(status="active", telegram_user_id=users.c.telegram_chat_id))
    owners = _owner_ids()
    if owners:
        op.execute(users.update().where(users.c.telegram_chat_id.in_(owners)).values(tier="owner"))


def downgrade() -> None:
    with op.batch_alter_table("outbox") as b:
        b.drop_index("ix_outbox_priority")
        b.drop_column("priority")
    with op.batch_alter_table("users") as b:
        b.drop_constraint("fk_users_invite_id", type_="foreignkey")
        b.drop_constraint("uq_users_composio_user_id", type_="unique")
        b.drop_index("ix_users_status")
        for col in ("is_test", "deleted_at", "inactive_since", "budget_override_usd_day", "ban_reason",
                    "banned_at", "activated_at", "invite_id", "composio_user_id", "country", "currency",
                    "locale", "telegram_user_id", "tier", "status"):
            b.drop_column(col)
    op.drop_table("llm_usage")
    op.drop_table("invite_redemptions")
    op.drop_table("invite_codes")
