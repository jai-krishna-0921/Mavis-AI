"""Outbox rows remember the channel they answer on (Slack chat handle); NULL keeps the default route."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_outbox_route"
down_revision = "0015_native_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("route", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "route")
