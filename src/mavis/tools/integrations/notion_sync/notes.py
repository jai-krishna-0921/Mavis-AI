# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Reading "Your notes": what the user types under that heading of a Mavis page becomes context for Mavis.

For the pages of one database whose last-edited time moved since the last read, the blocks below the "Your
notes" heading are read and queued as LEARN jobs under a `notion:` source reference (so forget and
disconnect erase what they taught). Text in blocks last edited by the connecting Notion user (`owner_id`)
is learned as the user's own; text anyone else put there is learned as third-party. The page's edit time is
kept in the link's meta, so an unchanged page costs nothing.
"""

from __future__ import annotations

import structlog

from mavis.bus import get_bus
from mavis.domain import timeutil
from mavis.domain.events import Job, JobKind
from mavis.memory import controls
from mavis.store.repo import notion_links
from mavis.tools.integrations.native.notion_api import NotionError, page_text
from mavis.tools.integrations.notion_sync import blocks
from mavis.tools.integrations.notion_sync.runner import SyncCtx

log = structlog.get_logger(__name__)

LIST_LIMIT = 5000
MAX_NOTES_CHARS = 3000


def _after_heading(children: list[dict]) -> list[dict]:
    for i, block in enumerate(children):
        kind = str(block.get("type") or "")
        if kind.startswith("heading_") and blocks.rich_plain_of(block).strip() == blocks.YOUR_NOTES:
            return children[i + 1:]
    return []


def _subject(kind: str, meta: dict) -> str:
    """Who or what the page is about, from the link: a name, or a meeting title and date."""
    if kind == "meeting":
        title = " ".join(str(meta.get("title") or "").split())[:100]
        return f"{title} ({str(meta.get('start') or '')[:10]})" if meta.get("start") else title
    return " ".join(str(meta.get("name") or "").split())[:100]


async def _learn(uid: int, page_id: str, label: str, text: str, own: bool) -> None:
    text = text.strip()[:MAX_NOTES_CHARS]
    if not text:
        return
    who = "the user" if own else "someone else with access to the page"
    ref = f"notion:notes:{page_id}:{'own' if own else 'other'}:{blocks.content_hash(text)[:12]}"
    await get_bus().enqueue(Job(
        id=f"learn:{ref}", user_id=uid, kind=JobKind.LEARN,
        payload={"text": f"Notes written by {who} on their Notion page about {label}:\n{text}",
                 "source_ref": ref, "trust": "user" if own else "untrusted", "conversation": False,
                 "anchor_at": timeutil.now().isoformat()}))


async def read_your_notes(ctx: SyncCtx, db: str, kind: str) -> None:
    """Read the changed pages of one Mavis database (`db`, link `kind`); stops quietly with the budget."""
    uid = ctx.user_id
    if "notion" in await controls.paused(uid):
        return  # the user paused learning from Notion (N3)
    links = {r.page_id: r for r in await notion_links.all_of(uid, kind)}
    if not links:
        return
    owner = str(ctx.account.get("owner_id") or "")
    for page in await ctx.api.query(uid, ctx.home.ds[db], limit=LIST_LIMIT):
        link = links.get(page.get("id"))
        edited = str(page.get("last_edited_time") or "")
        if link is None or link.meta.get("fresh") or link.meta.get("notes_at") == edited:
            continue
        if not ctx.left():
            return
        try:
            children = _after_heading(await ctx.api.children(uid, link.page_id, limit=500))
        except NotionError as exc:
            log.info("notion.notes_read_failed", kind=exc.kind.value)
            continue
        mine = [b for b in children if (b.get("last_edited_by") or {}).get("id") == owner and owner]
        theirs = [b for b in children if b not in mine]
        subject = _subject(kind, link.meta)
        label = f"{subject} ({kind})" if subject else kind
        await _learn(uid, link.page_id, label, page_text(mine), True)
        await _learn(uid, link.page_id, label, page_text(theirs), False)
        await notion_links.upsert(uid, kind, link.local_key, link.page_id,
                                  meta={**link.meta, "notes_at": edited})
