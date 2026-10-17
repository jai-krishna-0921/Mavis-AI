# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Block builders and the helpers that keep Mavis-owned blocks apart from the user's own."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mavis.domain.errors import FailureKind
from mavis.tools.integrations.native.notion_api import NotionApi, NotionError

YOUR_NOTES = "Your notes"
TEXT_LIMIT = 2000


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text}}] if text else []


def heading(text: str, level: int = 2) -> dict:
    kind = f"heading_{min(3, max(1, level))}"
    return {"type": kind, kind: {"rich_text": _rich(text[:TEXT_LIMIT])}}


def para(text: str) -> list[dict]:
    """One paragraph block per 2000 characters (Notion's limit for one rich text)."""
    parts = [text[i:i + TEXT_LIMIT] for i in range(0, len(text), TEXT_LIMIT)] or [""]
    return [{"type": "paragraph", "paragraph": {"rich_text": _rich(p)}} for p in parts]


def bullets(items: list[str]) -> list[dict]:
    return [{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich(i[:TEXT_LIMIT])}}
            for i in items]


def callout(text: str) -> dict:
    return {"type": "callout", "callout": {"rich_text": _rich(text[:TEXT_LIMIT])}}


def divider() -> dict:
    return {"type": "divider", "divider": {}}


def content_hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def replace_owned(api: NotionApi, user_id: int, page_id: str, old_ids: list[str],
                        new_blocks: list[dict]) -> list[str]:
    """Delete the blocks Mavis wrote before (already gone is fine) and write the new ones at the top of the
    page. Blocks the user added, anywhere, are never touched."""
    for block_id in old_ids:
        try:
            await api.delete_block(user_id, block_id)
        except NotionError as exc:
            if exc.kind not in (FailureKind.NOT_FOUND, FailureKind.INVALID_ARGUMENT):
                raise
    if not new_blocks:
        return []
    return await api.append(user_id, page_id, new_blocks, position={"type": "start"})


async def init_page_body(api: NotionApi, user_id: int, page_id: str, owned: list[dict]) -> list[str]:
    """A new page body: the owned blocks, then a divider and an empty "Your notes" section for the user.
    Returns only the owned ids."""
    ids = await api.append(user_id, page_id, [*owned, divider(), heading(YOUR_NOTES), *para("")])
    return ids[:len(owned)]
