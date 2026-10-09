"""Compact, model-friendly text for Gmail results.

Raw provider payloads are mostly MIME structure and base64; the 6000-char tool cap would cut them
before the body text. These renderers keep what a reader needs: ids (so the model can open a
message), sender, subject, date and the body text. The registry wraps the result as untrusted.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from typing import Any

from mavis.tools.integrations.normalize import extract_messages, normalize_email, pick

BODY_CHARS = 5500  # leaves room for the header lines under the registry's 6000-char cap
PREVIEW_CHARS = 280
_BLANKS = re.compile(r"\n\s*\n\s*\n+")
_SPACES = re.compile(r"[ \t ]+")
_URL = r"(?:https?://|www\.)[^\s<>()\[\]]+"
_PAREN_URL = re.compile(rf"\s*[(<\[]\s*{_URL}\s*[)>\]]")
_BARE_URL = re.compile(_URL)
_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def strip_urls(text: str) -> str:
    """Drop parenthesised/bracketed URLs and replace bare ones with [link]. Tracking links eat the body
    budget and widen the injection surface; summaries never need raw URLs."""
    return _BARE_URL.sub("[link]", _PAREN_URL.sub("", text))


def _clean(text: str) -> str:
    text = strip_urls(text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _SPACES.sub(" ", text)
    return _BLANKS.sub("\n\n", text).strip()


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def _from_parts(part: Any, mime: str) -> str:
    if not isinstance(part, dict):
        return ""
    if str(part.get("mimeType", "")).lower() == mime:
        data = pick(part, "body.data")
        if isinstance(data, str) and data:
            return _decode(data)
    for child in part.get("parts") or []:
        if text := _from_parts(child, mime):
            return text
    return ""


def body_text(msg: dict) -> str:
    """Best available plain text: messageText, then a text/plain or text/html part, then the preview."""
    text = pick(msg, "messageText", "message_text", "body", "text")
    if isinstance(text, str) and text.strip():
        return _clean(text)
    payload = msg.get("payload")
    if plain := _from_parts(payload, "text/plain"):
        return _clean(plain)
    if rich := _from_parts(payload, "text/html"):
        return _clean(html.unescape(_TAGS.sub(" ", rich)))
    return _clean(str(pick(msg, "preview.body", "snippet", default="")))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " ...[truncated]"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_search(data: Any, args: Any = None) -> str:
    msgs = extract_messages(data)
    if not msgs:
        # a neutral fact naming the query; what to make of it is in the chat rules (TOOL_RULES)
        query = " ".join(str(getattr(args, "query", "") or "").split())
        return f"No emails matched the query {query!r}." if query else "No emails matched the query."
    lines = [f"{len(msgs)} email(s). Call mail_read with a message_id to read one in full."]
    for raw in msgs:
        m = normalize_email(raw)
        unread = " (unread)" if "UNREAD" in m["labels"] else ""
        preview = _clip(_one_line(body_text(raw)), PREVIEW_CHARS)
        lines.append(
            f"- message_id={m['message_id']} thread_id={m['thread_id']}{unread}\n"
            f"  From: {m['from']}\n  Subject: {m['subject']}\n  Date: {m['received_at'] or 'unknown'}\n"
            f"  Preview: {preview}"
        )
    return "\n".join(lines)


def _single(data: Any) -> dict | None:
    if isinstance(data, dict):
        for key in ("data", "message", "response_data"):
            inner = data.get(key)
            if isinstance(inner, dict) and not pick(data, "messageId", "id"):
                return _single(inner)
        if msgs := extract_messages(data):
            return msgs[0]
        return data
    return None


def render_read(data: Any) -> str:
    raw = _single(data)
    if raw is None:
        return json.dumps(data, default=str, ensure_ascii=False)[:BODY_CHARS]
    m = normalize_email(raw)
    body = _clip(body_text(raw), BODY_CHARS) or "(no text body)"
    return (
        f"message_id={m['message_id']} thread_id={m['thread_id']}\n"
        f"From: {m['from']}\nTo: {m['to']}\nSubject: {m['subject']}\n"
        f"Date: {m['received_at'] or 'unknown'}\n\n{body}"
    )


def render_change(data: Any) -> str:
    """Result of an organising action: what was done to how many, never the mail itself."""
    if not isinstance(data, dict):
        return "Done."
    verb = str(data.get("verb") or "Updated")
    bits = []
    if data.get("messages"):
        bits.append(f"{data['messages']} email(s)")
    if data.get("threads"):
        bits.append(f"{data['threads']} conversation(s)")
    text = f"Done. {verb} {' and '.join(bits) or 'the mail'}."
    if data.get("added"):
        text += f" Added label ids: {', '.join(map(str, data['added']))}."
    if data.get("removed") and verb.startswith("Updated"):
        text += f" Removed label ids: {', '.join(map(str, data['removed']))}."
    return text


def render_labels(data: Any) -> str:
    labels = data.get("labels") if isinstance(data, dict) else None
    if labels is None:
        return render_change(data)
    mine = [x for x in labels if isinstance(x, dict) and x.get("type") == "user"]
    system = [x for x in labels if isinstance(x, dict) and x.get("type") != "user"]
    lines = [f"{len(labels)} label(s). Use label names with mail_label."]
    lines += [f"- {x.get('name', '')} (id {x.get('id', '')})" for x in mine]
    lines.append("System: " + ", ".join(str(x.get("name", "")) for x in system))
    return "\n".join(lines)


RENDERERS = {
    "mail.search": render_search, "mail.read": render_read, "mail.archive": render_change,
    "mail.mark_read": render_change, "mail.mark_unread": render_change, "mail.trash": render_change,
    "mail.untrash": render_change, "mail.label": render_labels,
}
