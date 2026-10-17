# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Compact, model-facing text for Fathom results.

Meeting titles, attendee names, summaries and action items are all written by other people: titles are
flattened to one line and summary text has its URLs replaced and is capped. The registry wraps the whole
result as untrusted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from mavis.tools.integrations.mail_render import strip_urls
from mavis.tools.integrations.workspace_render import one_line

SUMMARY_CHARS = 8000
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


def render_meetings(data: Any) -> str:
    rows = [r for r in data or [] if isinstance(r, dict)]
    if not rows:
        return "No Fathom meetings in that period."
    return "\n".join(
        f"- id={r.get('recording_id', '')} | {one_line(r.get('title') or '(untitled)')} | "
        f"{str(r.get('start') or '')[:16]} | "
        f"with {one_line(', '.join(r.get('attendees') or []) or 'no one listed')}"
        f" | {r.get('action_items_count', 0)} action items" for r in rows)


def render_meeting(data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    found = data.get("found", True)
    head = (f"{one_line(data.get('title') or '(untitled)')} | {str(data.get('start') or '')[:16]}" if found
            else "Title, time and action items could not be loaded for this recording (not among the "
                 "recent meetings); only the summary is available.")
    text = _BLANKS.sub("\n\n", strip_urls(str(data.get("summary") or ""))).strip()
    if len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS].rstrip() + " ...[truncated]"
    parts = [head, text or "(no summary yet)"]
    items = [i for i in data.get("action_items") or [] if isinstance(i, dict)]
    if items and found:
        parts.append("Action items:")
        for i in items:
            who = one_line(i.get("assignee") or "") or "unassigned"
            mark = "x" if i.get("completed") else " "
            parts.append(f"- [{mark}] {one_line(strip_urls(str(i.get('description') or '')))} ({who})")
    return "\n".join(parts)


FATHOM_RENDERERS: dict[str, Callable[..., Any]] = {
    "fathom.meetings": render_meetings, "fathom.meeting": render_meeting,
}
