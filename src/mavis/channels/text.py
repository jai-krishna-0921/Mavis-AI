from __future__ import annotations

import re

TELEGRAM_LIMIT = 4096
_FENCE = re.compile(r"^\s*```")
_REOPEN = "```\n"
_CLOSE = "\n```"


def _open_fence_at(text: str, end: int) -> bool:
    """True if a ``` fence is still open after text[:end]."""
    return sum(1 for line in text[:end].split("\n") if _FENCE.match(line)) % 2 == 1


def _closed_newline_cut(text: str, limit: int) -> int:
    """Last newline cut in the back half of the window that is not inside a fence, or -1."""
    cut = text.rfind("\n", 0, limit)
    while cut >= limit // 2:
        if not _open_fence_at(text, cut):
            return cut
        cut = text.rfind("\n", 0, cut)
    return -1


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on newlines, then spaces, then hard-cut, so each chunk fits the provider limit.

    Fence-aware: avoids cutting inside ``` blocks; when a block must be cut, the chunk closes
    the fence and the next chunk reopens it.
    """
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = _closed_newline_cut(text, limit)
        if cut >= 0:
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
            continue
        room = limit - len(_CLOSE) if _open_fence_at(text, limit) else limit
        cut = text.rfind("\n", 0, room)
        if cut < room // 2:
            cut = text.rfind(" ", 0, room)
        if cut < room // 2:
            cut = room
        head, text = text[:cut].rstrip(), text[cut:].lstrip()
        if _open_fence_at(head, len(head)):
            head += _CLOSE
            text = _REOPEN + text
        chunks.append(head)
    chunks.append(text)
    return chunks
