# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The Briefings pass: each morning brief Mavis sent becomes one page in the Briefings database.

A brief is an assistant message whose event id starts with `proactive:morning:` and carries the user's local
date. One page per date, written once (a brief is a record, never edited); link kind `briefing`, local key =
the date. The pass is daily and is forced right after a brief goes out (`NotionBriefSource.delivered`).
"""

from __future__ import annotations

import re
from datetime import timedelta

import structlog

from mavis.store.repo import messages, notion_links
from mavis.tools.integrations.native.tokens import ACTIVE
from mavis.tools.integrations.notion_sync import blocks, rows
from mavis.tools.integrations.notion_sync.blocks import TEXT_LIMIT
from mavis.tools.integrations.notion_sync.runner import (
    SyncCtx,
    enqueue_sync,
    notion_grant,
    register_pass,
)

log = structlog.get_logger(__name__)

LINK = "briefing"
PREFIX = "proactive:morning:"
WINDOW = timedelta(days=2)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:TEXT_LIMIT]}}] if text else []


def _body(text: str) -> list[dict]:
    return [b for line in text.splitlines() if line.strip() for b in blocks.para(line.strip())]


async def briefings_pass(ctx: SyncCtx) -> None:
    uid = ctx.user_id
    seen: set[str] = set()
    for msg in await messages.proactive_since(uid, PREFIX, ctx.now - WINDOW):
        found = DATE.search(msg.event_id or "")
        if found is None or found.group() in seen:
            continue
        day = found.group()
        seen.add(day)
        link = await notion_links.get(uid, LINK, day)
        if link is not None and not link.meta.get("fresh"):
            continue
        if not ctx.left():
            return
        if link is None:
            props = {
                "Name": {"title": _rich(f"Briefing {day}")},
                "Mavis key": {"rich_text": _rich(f"briefing:{day}")},
                "Date": {"date": {"start": day}},
            }
            page = await rows.create_row(ctx.api, uid, ctx.home, "briefings", LINK, day, props)
            # link first: if the body write fails, the retry finishes this page instead of making another
            await notion_links.upsert(uid, LINK, day, page["id"], pushed_hash="", meta={"fresh": True})
            await rows.created(uid, LINK, day)
            page_id = page["id"]
        else:
            page_id = link.page_id
        await ctx.api.append(uid, page_id, _body(msg.content))
        await notion_links.upsert(uid, LINK, day, page_id, pushed_hash="", meta={"fresh": False})


class NotionBriefSource:
    """Adds nothing to the brief. Once a brief went out it asks for the Briefings pass, so the page appears
    within minutes of the message."""

    name = "notion"

    async def items(self, user_id, start, end) -> list:
        return []

    async def delivered(self, user_id, at) -> None:
        grant = await notion_grant(user_id)
        if grant is not None and grant.status == ACTIVE:
            await enqueue_sync(user_id, force={"briefings"})


register_pass("briefings", timedelta(days=1), briefings_pass, order=50)
