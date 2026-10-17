# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Compact, model-facing text for Notion results.

Titles and page text come from other people as often as from the user: titles are flattened to one line,
page text has its URLs replaced and is capped. The registry wraps the whole result as untrusted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from mavis.tools.integrations.mail_render import strip_urls
from mavis.tools.integrations.workspace_render import one_line

TEXT_CHARS = 20000
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


def render_search(data: Any) -> str:
    rows = [r for r in data or [] if isinstance(r, dict)]
    if not rows:
        return "No Notion pages matched."
    return "\n".join(
        f"- id={r.get('id', '')} | {one_line(r.get('title') or '(untitled)')} | {r.get('kind', '')} | "
        f"edited {str(r.get('edited') or '')[:10]} | {r.get('url', '')}" for r in rows)


def render_read(data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    head = f"{one_line(data.get('title') or '(untitled)')} | {data.get('url', '')}"
    text = _BLANKS.sub("\n\n", strip_urls(str(data.get("text") or ""))).strip()
    if len(text) > TEXT_CHARS:
        text = text[:TEXT_CHARS].rstrip() + " ...[truncated]"
    return f"{head}\n{text}" if text else f"{head}\n(the page has no text)"


def render_created(data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    return f"Created the Notion page {one_line(data.get('title') or '(untitled)')} ({data.get('url', '')})."


def render_appended(data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    return f"Added {data.get('added', 0)} paragraphs to the Notion page."


NOTION_RENDERERS: dict[str, Callable[..., Any]] = {
    "notion.search": render_search, "notion.read": render_read,
    "notion.create_page": render_created, "notion.append": render_appended,
}
