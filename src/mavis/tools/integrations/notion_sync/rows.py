# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Shared row plumbing for the passes: property ids, the one "gone" check, and creating a row exactly once.

The user may rename any column of a Mavis database, so every write goes out keyed by the stored property id
(`Home.props`) and every read matches the page's values by id, then hands the pass a copy keyed by the
canonical names the pass knows. An error on a write never means "the row is gone" by itself: `page_gone`
asks Notion (404 or in the trash) and anything else is raised.

A create whose result is unknown (a 5xx after the request was sent) leaves an `attempt` link behind. The
next try looks the row up by its `Mavis key` value before creating, so it is found again, not duplicated.
"""

from __future__ import annotations

from typing import Any

from mavis.domain.errors import FailureKind
from mavis.memory import itemids
from mavis.store.repo import notion_links
from mavis.tools.integrations.native.notion_api import NotionApi, NotionError
from mavis.tools.integrations.notion_sync.blocks import content_hash
from mavis.tools.integrations.notion_sync.home import SCHEMAS, Home

ATTEMPT = "attempt"
KEY_PROP = "Mavis key"


def is_suppressed(key: str, suppressed: set[str]) -> bool:
    """True when the user asked Mavis to forget this person key (an email, a Slack id or a name)."""
    return key in suppressed or itemids.person_id(key) in suppressed


def wire(home: Home, db: str, props: dict) -> dict:
    """Properties keyed by their stored ids (a property not yet mapped keeps its name)."""
    ids = home.props.get(db) or {}
    return {ids.get(name, name): value for name, value in props.items()}


def canonical(home: Home, db: str, page: dict) -> dict:
    """A copy of `page` whose properties are keyed by canonical name. Matching is by property id; a column
    the user added keeps its own name, unless that name would shadow a canonical one."""
    ids = {pid: name for name, pid in (home.props.get(db) or {}).items()}
    out: dict[str, Any] = {}
    for name, value in (page.get("properties") or {}).items():
        mapped = ids.get(value.get("id")) if isinstance(value, dict) else None
        if mapped:
            out[mapped] = value
        elif name not in SCHEMAS[db]:
            out[name] = value
    # a canonical column the page lacks (just created, or not mapped yet) falls back to its name
    for name in SCHEMAS[db]:
        if name not in out and name in (page.get("properties") or {}) and name not in ids.values():
            out[name] = page["properties"][name]
    return {**page, "properties": out}


async def page_gone(api: NotionApi, user_id: int, page_id: str) -> bool:
    """True when the page is deleted or in the trash. Any other failure is raised, never read as gone."""
    try:
        page = await api.page(user_id, page_id)
    except NotionError as exc:
        if exc.kind is FailureKind.NOT_FOUND:
            return True
        raise
    return bool(page.get("in_trash") or page.get("is_archived"))


async def update_row(api: NotionApi, user_id: int, home: Home, db: str, page_id: str,
                     props: dict) -> dict | None:
    """Update a row's properties; None when the row is gone (the caller then makes a new one). A rejected
    update on a row that still exists is raised."""
    try:
        return await api.update_page(user_id, page_id, properties=wire(home, db, props))
    except NotionError as exc:
        if exc.kind not in (FailureKind.NOT_FOUND, FailureKind.INVALID_ARGUMENT):
            raise
        if exc.kind is FailureKind.NOT_FOUND or await page_gone(api, user_id, page_id):
            return None
        raise


def _attempt_key(kind: str, key: str) -> tuple[str, str]:
    digest = content_hash([kind, key])
    return f"{kind}:{key}" if len(kind) + len(key) < 190 else digest, f"{ATTEMPT}:{digest}"


def _key_text(props: dict) -> str:
    items = (props.get(KEY_PROP) or {}).get("rich_text") or []
    return "".join(str(i.get("text", {}).get("content", "")) for i in items)


async def create_row(api: NotionApi, user_id: int, home: Home, db: str, kind: str, key: str,
                     props: dict) -> dict:
    """Create the row once. If an earlier create may have saved (an attempt link is still there), look the
    row up by its Mavis key and adopt it. Call `created` after the real link is saved."""
    local, page_id = _attempt_key(kind, key)
    text = _key_text(props)
    if text and await notion_links.get(user_id, ATTEMPT, local) is not None:
        found = await api.query(
            user_id, home.ds[db], limit=5,
            filter={"property": (home.props.get(db) or {}).get(KEY_PROP, KEY_PROP),
                    "rich_text": {"equals": text}})
        live = [p for p in found if not (p.get("in_trash") or p.get("is_archived"))]
        if live:
            return live[0]
    await notion_links.upsert(user_id, ATTEMPT, local, page_id, meta={"db": db})
    return await api.create_page(
        user_id, {"type": "data_source_id", "data_source_id": home.ds[db]}, wire(home, db, props))


async def created(user_id: int, kind: str, key: str) -> None:
    await notion_links.delete(user_id, ATTEMPT, _attempt_key(kind, key)[0])
