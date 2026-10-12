# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Outbox dedupe keys and message event ids are unique per user, not across all users.

Daily keys ("morning:<date>", "evening:<date>") are the same string for every user: with one global unique
key, the second user's check-in found the first user's row, was treated as already queued and never sent,
and the partial-send recovery copied the other user's text into this user's history. Proactive message
event ids are built from the same keys, so the second user's message could not be logged either.
"""

from __future__ import annotations

from alembic import op

revision = "0023_outbox_dedupe_per_user"
down_revision = "0022_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outbox") as batch:
        batch.drop_constraint("uq_outbox_dedupe_key", type_="unique")
        batch.create_unique_constraint("uq_outbox_user_dedupe", ["user_id", "dedupe_key"])
    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("uq_messages_event_id", type_="unique")
        batch.create_unique_constraint("uq_messages_user_event", ["user_id", "event_id"])


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("uq_messages_user_event", type_="unique")
        batch.create_unique_constraint("uq_messages_event_id", ["event_id"])
    with op.batch_alter_table("outbox") as batch:
        batch.drop_constraint("uq_outbox_user_dedupe", type_="unique")
        batch.create_unique_constraint("uq_outbox_dedupe_key", ["dedupe_key"])
