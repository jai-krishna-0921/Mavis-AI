# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Compact, model-facing text for Microsoft 365 results (Teams chats and OneDrive files).

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


def _when(value: Any) -> str:
    return str(value or "")[:10]


def render_files(data: Any) -> str:
    files = [f for f in (data or {}).get("files") or [] if isinstance(f, dict)]
    if not files:
        return "No OneDrive files matched."
    lines = []
    for f in files:
        who = one_line(f.get("modifiedBy") or "unknown", NAME_CHARS)
        line = (f"- id={f.get('id', '')} | {one_line(f.get('name') or '(untitled)')} | "
                f"{f.get('mimeType', '')} | {f.get('size', '')} | modified {_when(f.get('modifiedTime'))} "
                f"by {who}")
        if f.get("shared") and f.get("sharedBy"):
            line += f" | shared by {one_line(f['sharedBy'], NAME_CHARS)}"
        lines.append(line)
    return "\n".join(lines)


def render_read(data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    name = one_line(data.get("name") or "(untitled)")
    head = f"{name} | {data.get('mimeType', '')} | {data.get('size', '')}"
    parts = [head]
    if data.get("note"):
        parts.append(f"({one_line(data['note'], LAST_CHARS)})")
    text = clip_body(str(data.get("text") or ""))
    if text:
        parts.append(text)
    if data.get("truncated"):
        parts.append("(the file is longer; only the start is shown)")
    return "\n".join(parts)


MICROSOFT_RENDERERS: dict[str, Callable[..., Any]] = {
    "teams.chats": render_chats, "teams.history": render_history,
    "onedrive.search": render_files, "onedrive.list_recent": render_files, "onedrive.read": render_read,
}
