"""phase A: loops.trust and loops.origin (explicit provenance instead of source-prefix inference)

Existing rows read as untrusted with an unknown origin (the safe default). Backfill, from writer
constants only (never from origin prefixes):
- `onboarding`: the morning routine Mavis seeds itself, with a fixed title and no outside content, so
  system trust and the routine origin.
- `tool:track_loop`: the chat tool wrote it, so the origin is conversation. Its trust stays untrusted:
  whether the turn that called it had read third-party content is not recorded on old rows (and the
  approval gate for tainted turns did not exist for all of them), so trust is not determinable.
Rows extracted by LEARN from a chat turn cannot be told apart from laundered email content either.
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
    op.execute("UPDATE loops SET origin = 'conversation' WHERE source = 'tool:track_loop'")
    op.execute("UPDATE loops SET trust = 'system', origin = 'routine' WHERE source = 'onboarding'")


def downgrade() -> None:
    with op.batch_alter_table("loops") as batch:
        batch.drop_column("origin")
        batch.drop_column("trust")
