"""Telegram message ids per chat, so /clear can delete recent messages (ids only, no text)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_chat_message_ids"
down_revision = "0018_personal_layer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_message_ids",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("sent_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_chat_message_ids_chat_msg"),
    )
    op.create_index("ix_chat_message_ids_chat_sent", "chat_message_ids", ["chat_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_message_ids_chat_sent", table_name="chat_message_ids")
    op.drop_table("chat_message_ids")
