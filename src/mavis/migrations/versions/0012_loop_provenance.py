"""phase A: loops.trust and loops.origin (explicit provenance instead of source-prefix inference)

Existing rows read as untrusted with an unknown origin, except the two writers whose `source` is a
fixed constant naming the writer itself (not an origin prefix): the chat `track_loop` tool (the user
asked for it in chat, approving it when the turn was tainted) and the onboarding routine (Mavis).
Rows extracted by LEARN from a chat turn cannot be told apart from laundered email content, so they
stay untrusted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_loop_provenance"
down_revision = "0011_attention_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("loops") as batch:
        batch.add_column(sa.Column("trust", sa.String(12), nullable=False, server_default="untrusted"))
        batch.add_column(sa.Column("origin", sa.String(16), nullable=False, server_default="unknown"))
    op.execute("UPDATE loops SET trust = 'user', origin = 'conversation' WHERE source = 'tool:track_loop'")
    op.execute("UPDATE loops SET trust = 'system', origin = 'routine' WHERE source = 'onboarding'")


def downgrade() -> None:
    with op.batch_alter_table("loops") as batch:
        batch.drop_column("origin")
        batch.drop_column("trust")
