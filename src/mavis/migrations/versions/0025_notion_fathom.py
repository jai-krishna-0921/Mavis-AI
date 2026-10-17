# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Notion and Fathom connectors: the page links Mavis keeps and the meeting notes it joins to recordings."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_notion_fathom"
down_revision = "0024_mcp_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notion_links",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("local_key", sa.String(200), nullable=False),
        sa.Column("page_id", sa.String(64), nullable=False),
        sa.Column("pushed_hash", sa.String(64), nullable=False),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "kind", "local_key", name="uq_notion_links_user_kind_key"),
        sa.UniqueConstraint("user_id", "page_id", name="uq_notion_links_user_page"),
    )
    op.create_index("ix_notion_links_user_id", "notion_links", ["user_id"])
    op.create_table(
        "meeting_notes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_key", sa.String(200), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attendees", sa.JSON, nullable=False),
        sa.Column("recording_id", sa.String(32), nullable=True),
        sa.Column("fathom_url", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("action_items", sa.JSON, nullable=False),
        sa.Column("user_notes", sa.Text, nullable=False),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "event_key", name="uq_meeting_notes_user_event"),
        sa.UniqueConstraint("user_id", "recording_id", name="uq_meeting_notes_user_recording"),
    )
    op.create_index("ix_meeting_notes_user_id", "meeting_notes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_meeting_notes_user_id", table_name="meeting_notes")
    op.drop_table("meeting_notes")
    op.drop_index("ix_notion_links_user_id", table_name="notion_links")
    op.drop_table("notion_links")
