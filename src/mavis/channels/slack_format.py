"""Markdown (as the model writes it) to Slack mrkdwn and Block Kit. Pure functions, no I/O.

Slack mrkdwn: *bold*, _italic_, `code`, <url|text> links, and & < > must be escaped as entities.
Dashes are normalised and verbatim spans (approval previews) are kept exactly, like the Telegram renderer.
"""

from __future__ import annotations

import re

from mavis.channels import formatting as fmt
from mavis.domain.messages import Button

SECTION_LIMIT = 2900  # a section block holds 3000 characters
BUTTON_LABEL_LIMIT = 75
BUTTONS_PER_ROW = 5
MAX_ACTION_ROWS = 4  # keeps the whole message well inside the 50 block cap


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    slots: list[str] = []

    def hold(markup: str) -> str:
        slots.append(markup)
        return f"\x00{len(slots) - 1}\x00"

    text = text.replace("\x00", "")
    text = fmt._CODE.sub(lambda m: hold(f"`{_esc(m.group(1))}`"), text)
    text = fmt._LINK.sub(lambda m: hold(f"<{_esc(m.group(2))}|{_esc(m.group(1))}>"), text)
    text = fmt._URL.sub(lambda m: hold(f"<{_esc(m.group(0))}>"), text)
    text = _esc(text)
    text = fmt._BOLD.sub(lambda m: hold(f"*{m.group(1) or m.group(2)}*"), text)
    text = fmt._ITALIC_STAR.sub(lambda m: hold(f"_{m.group(1)}_"), text)
    text = fmt._ITALIC_UNDER.sub(lambda m: hold(f"_{m.group(1)}_"), text)
    return fmt._SLOT.sub(lambda m: slots[int(m.group(1))], text)


def to_mrkdwn(markdown: str) -> str:
    md, spans = fmt._hold_verbatim(markdown.replace("\r\n", "\n"))
    out: list[str] = []
    code: list[str] | None = None
    for raw in md.split("\n"):
        if code is not None and not fmt._FENCE.match(raw):
            code.append(raw)
            continue
        if fmt._FENCE.match(raw):
            if code is None:
                code = []
            else:
                out.append("```\n" + _esc("\n".join(code)) + "\n```")
                code = None
            continue
        line = fmt.normalize_line(raw)
        if fmt._HR.match(line) or (fmt._TABLE_SEP.match(line) and "|" in line):
            continue
        if m := fmt._HEADING.match(line):
            body = fmt._strip_inline(m.group(1))
            out.append(f"*{_esc(body)}*")
        elif fmt._TABLE_ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append(_inline(" | ".join(c for c in cells if c)))
        elif m := fmt._BULLET.match(line):
            out.append(f"{m.group(1)}• {_inline(m.group(2))}")
        else:
            out.append(_inline(line))
    if code is not None:
        out.append("```\n" + _esc("\n".join(code)) + "\n```")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return fmt._HELD.sub(lambda m: _esc(spans[int(m.group(1))]), text)


def plain_fallback(markdown: str) -> str:
    """Notification text for a message that carries blocks."""
    return fmt.to_plain(markdown)[:300] or "Mavis"


def _element(b: Button, i: int, j: int) -> dict:
    el: dict = {"type": "button", "text": {"type": "plain_text", "text": b.label[:BUTTON_LABEL_LIMIT],
                                           "emoji": True}}
    if b.url:
        el["url"] = b.url
        el["action_id"] = f"url:{i}:{j}"
    else:
        el["action_id"] = f"{b.data}"[:255]
        el["value"] = b.data
    return el


def action_blocks(buttons: list[list[Button]] | None) -> list[dict]:
    blocks: list[dict] = []
    for i, row in enumerate((buttons or [])[:MAX_ACTION_ROWS]):
        elements = [_element(b, i, j) for j, b in enumerate(row[:BUTTONS_PER_ROW]) if b.url or b.data]
        if elements:
            blocks.append({"type": "actions", "block_id": f"mavis_actions_{i}", "elements": elements})
    return blocks


def message_blocks(mrkdwn_chunks: list[str], buttons: list[list[Button]] | None) -> list[dict]:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in mrkdwn_chunks if c.strip()]
    return blocks + action_blocks(buttons)
