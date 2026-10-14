# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous
# Research, MIT License.
"""A tool call's result as the text the model reads.

Adapted from the ideas of Hermes Agent `tools/mcp_tool_handlers.py` (`_render_call_tool_result`,
`_render_content_blocks`, `_content_dual_emits_structured`) and `tools/mcp_tool_content.py`: text blocks are
joined; invisible characters are stripped; `structuredContent` fills the result when there is no usable text
and is dropped when a text block is just its JSON; binary content is never passed on, a one-line note says it
was left out; an `isError` result becomes an error whose text has credentials blanked. Hermes caches images
and audio as files and forwards `_meta`; Mavis does neither (no media pipeline, less untrusted surface).

The output is capped at 12,000 characters, 40% head and 60% tail around a marker. The registry fences it
(`untrusted_output=True`) and taints the run; nothing here makes the text trustworthy."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mavis.mcp.limits import MAX_RESULT_CHARS
from mavis.mcp.text import REDACTED, clean_description, clean_text, sanitize_error, truncate_head_tail

EMPTY_RESULT = "(the tool returned no content)"
ERROR_FALLBACK = "The tool returned an error with no message."
_HARD_CAP = 1_000_000  # characters kept before assembling: a response is at most 4 MB on the wire


@dataclass(frozen=True)
class Rendered:
    text: str
    is_error: bool = False


def _note(kind: str, detail: str = "") -> str:
    return f"[{kind} omitted{f': {detail}' if detail else ''}]"


def _resource_text(block: Any) -> str | None:
    """Text of an embedded resource, labelled by its (cleaned) uri; a binary one is a note."""
    resource = getattr(block, "resource", None)
    uri = clean_description(getattr(resource, "uri", ""), 200)
    text = getattr(resource, "text", None)
    if isinstance(text, str):
        body = clean_text(text)
        return f"[resource {uri}]\n{body}" if uri else body
    mime = clean_description(getattr(resource, "mime_type", "") or "", 60)
    return _note("binary resource", ", ".join(p for p in (uri, mime) if p))


def _block_text(block: Any) -> tuple[str, bool]:
    """(rendered text, whether it counts as usable text for the structuredContent decision)."""
    kind = getattr(block, "type", None)
    text = getattr(block, "text", None)
    if kind == "text" or isinstance(text, str):
        cleaned = clean_text(text or "")
        return cleaned, bool(cleaned.strip())
    if kind == "image":
        return _note("image", clean_description(getattr(block, "mime_type", ""), 60)), False
    if kind == "audio":
        return _note("audio", clean_description(getattr(block, "mime_type", ""), 60)), False
    if kind == "resource_link":
        name = clean_description(getattr(block, "name", ""), 100)
        uri = clean_description(getattr(block, "uri", ""), 300)
        return f"[resource link: {uri}{f' ({name})' if name else ''}]", False
    if kind == "resource":
        rendered = _resource_text(block) or ""
        return rendered, rendered != "" and not rendered.startswith("[binary resource")
    return _note(clean_description(str(kind or "content block"), 40) + " block"), False


def _same_json(text: str, structured: Any) -> bool:
    """A text block that is exactly the structured content serialized: compared as parsed JSON, so
    whitespace, indentation and key order do not matter."""
    try:
        return json.loads(text) == structured
    except (TypeError, ValueError):
        return False


def _dump(structured: Any) -> str:
    try:
        return json.dumps(structured, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(structured)


def _error_text(blocks: Iterable[Any]) -> str:
    parts = []
    for block in blocks:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            text = getattr(getattr(block, "resource", None), "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(clean_text(text))
    return "\n".join(parts)


def _blank_secrets(text: str, secrets: Iterable[str]) -> str:
    """Exact-match redaction of the secrets we sent. Success text gets only this, not the credential-shape
    patterns (they would mangle ordinary results)."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, REDACTED)
    return text


def render_call_result(result: Any, *, secrets: Iterable[str] = (),
                       limit: int = MAX_RESULT_CHARS) -> Rendered:
    """`result` is an `mcp_types.CallToolResult` (or anything shaped like one)."""
    blocks = list(getattr(result, "content", None) or [])
    if getattr(result, "is_error", False):
        body = _error_text(blocks).strip() or ERROR_FALLBACK
        body = truncate_head_tail(sanitize_error(body, secrets)[:_HARD_CAP], limit, label="ERROR")
        return Rendered(body, is_error=True)
    parts: list[str] = []
    usable = 0
    texts: list[str] = []
    for block in blocks:
        rendered, counts = _block_text(block)
        if rendered:
            parts.append(rendered)
        if counts:
            usable += 1
            texts.append(rendered)
    structured = getattr(result, "structured_content", None)
    if structured is not None and not (usable and any(_same_json(t, structured) for t in texts)):
        dumped = clean_text(_dump(structured)[:_HARD_CAP])
        parts.append(dumped if not usable else f"[structured content]\n{dumped}")
    text = _blank_secrets("\n".join(parts).strip(), secrets)
    if not text:
        return Rendered(EMPTY_RESULT)
    return Rendered(truncate_head_tail(text[:_HARD_CAP], limit, label="RESULT"))
