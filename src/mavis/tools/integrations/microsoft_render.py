# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Compact, model-facing text for Microsoft 365 results (Teams today; OneDrive joins here).

Names and message text come from other people: each is flattened to one line or clipped, and the registry
wraps the whole result as untrusted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mavis.tools.integrations.workspace_render import clip_body, one_line

LAST_CHARS = 120
NAME_CHARS = 60


def _others(chat: dict) -> str:
    names = [one_line(m.get("name") or m.get("email"), NAME_CHARS) for m in chat.get("members") or []
             if isinstance(m, dict) and not m.get("me")]
    return ", ".join(n for n in names if n)


def render_chats(data: Any) -> str:
    chats = [c for c in (data or {}).get("chats") or [] if isinstance(c, dict)]
    if not chats:
        return "No Teams chats found."
    lines = []
    for c in chats:
        title = one_line(c.get("topic"), NAME_CHARS) or _others(c) or "(no title)"
        line = (f"- chat_id={c.get('id', '')} | {title} | {c.get('type', '')} | "
                f"last {c.get('lastMessageAt', '')}")
        last = c.get("lastMessage")
        if isinstance(last, dict):
            line += f": {one_line(last.get('from'), NAME_CHARS)}: {one_line(last.get('text'), LAST_CHARS)}"
        lines.append(line)
    return "\n".join(lines)


def render_history(data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    msgs = sorted((m for m in data.get("messages") or [] if isinstance(m, dict)),
                  key=lambda m: str(m.get("at") or ""))
    lines = []
    for m in msgs:
        who = "me" if m.get("fromMe") else one_line(m.get("from"), NAME_CHARS) or "unknown"
        lines.append(f"[{m.get('at', '')}] {who}: {clip_body(str(m.get('text') or ''))}")
    if not lines:
        lines.append("No messages.")
    if data.get("skipped"):
        lines.append(f"({data['skipped']} system, deleted or app messages left out)")
    if data.get("next"):
        lines.append(f"More older messages: pass before={data['next']}")
        lines.append(f"next={data['next']}")
    return "\n".join(lines)


MICROSOFT_RENDERERS: dict[str, Callable[..., Any]] = {
    "teams.chats": render_chats, "teams.history": render_history,
}
