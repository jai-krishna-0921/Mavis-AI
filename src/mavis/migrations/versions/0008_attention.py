"""attention layer: observations, senders, money baselines, prefs

Merge order: qa-hardening (0006_initiative_decisions), Phase 4 (0007_orchestrator), then this revision.
If this lands before Phase 4, set down_revision = "0006_initiative_decisions". On a branch where neither
exists yet, point it at that branch's head (e.g. "0005_integrations") and re-point at merge time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_attention"
down_revision = "0006_initiative_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attention_observations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("message_id", sa.String(200), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("origin", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("method", sa.String(12), nullable=False),
        sa.Column("sender_domain", sa.String(120), nullable=False),
        sa.Column("sender_name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("needs_user", sa.Boolean, nullable=False),
        sa.Column("verdict", sa.String(12), nullable=False),
        sa.Column("urgency", sa.Integer, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("reasons", sa.JSON, nullable=False),
        sa.Column("facts", sa.JSON, nullable=False),
        sa.Column("summary", sa.String(240), nullable=False),
        sa.Column("action", sa.String(160), nullable=False),
        sa.Column("point_id", sa.String(64), nullable=True),
        sa.Column("pending_payload", sa.JSON, nullable=True),
        sa.Column("delivery", sa.String(12), nullable=False),
        sa.Column("feedback", sa.String(12), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "message_id", name="uq_attention_observations_user_msg"),
    )
    op.create_index("ix_attention_observations_user_id", "attention_observations", ["user_id"])
    op.create_index("ix_attention_observations_status", "attention_observations", ["status"])
    op.create_index("ix_attention_observations_created_at", "attention_observations", ["created_at"])
    op.create_table(
        "attention_senders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("address", sa.String(200), nullable=False),
        sa.Column("domain", sa.String(120), nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "address", name="uq_attention_senders_user_address"),
    )
    op.create_index("ix_attention_senders_user_id", "attention_senders", ["user_id"])
    op.create_table(
        "attention_money_baselines",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("amounts", sa.JSON, nullable=False),
        sa.Column("median", sa.Float, nullable=False),
        sa.Column("mad", sa.Float, nullable=False),
        sa.Column("hours", sa.JSON, nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "scope", "key", "currency", name="uq_attention_money_user_scope_key"),
    )
    op.create_index("ix_attention_money_baselines_user_id", "attention_money_baselines", ["user_id"])
    op.create_table(
        "attention_prefs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("observation_id", sa.Integer, nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("sentiment", sa.String(12), nullable=False),
        sa.Column("summary", sa.String(240), nullable=False),
        sa.Column("point_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_attention_prefs_user_id", "attention_prefs", ["user_id"])


def downgrade() -> None:
    op.drop_table("attention_prefs")
    op.drop_table("attention_money_baselines")
    op.drop_table("attention_senders")
    op.drop_table("attention_observations")
