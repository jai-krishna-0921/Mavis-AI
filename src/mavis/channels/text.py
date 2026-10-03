from __future__ import annotations

import re

TELEGRAM_LIMIT = 4096
_FENCE = re.compile(r"^\s*```")
_REOPEN = "```\n"
_CLOSE = "\n```"
_OPEN, _SHUT = "\x0e", "\x0f"  # formatting.VERBATIM_OPEN / VERBATIM_CLOSE


def _open_fence_at(text: str, end: int) -> bool:
    """True if a ``` fence is still open after text[:end]. Lines inside a verbatim span (an approval
    preview, quoted text) are content, not markdown, so their fences do not count."""
    count, inside = 0, False
    for line in text[:end].split("\n"):
        if not inside and not line.lstrip().startswith(_OPEN) and _FENCE.match(line):
            count += 1
        for c in line:
            if c == _OPEN:
                inside = True
            elif c == _SHUT:
                inside = False
    return count % 2 == 1


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
    return _balance_verbatim(chunks)


def _balance_verbatim(chunks: list[str]) -> list[str]:
    """A verbatim span cut across chunks is closed at the end of one and reopened in the next, so each
    chunk renders on its own without rewriting text that must reach the user unchanged."""
    out: list[str] = []
    inside = False
    for chunk in chunks:
        body = (_OPEN if inside else "") + chunk
        opens = [i for i, c in enumerate(body) if c in (_OPEN, _SHUT)]
        inside = bool(opens) and body[opens[-1]] == _OPEN
        out.append(body + (_SHUT if inside else ""))
    return out
