"""Markdown (as written by the model) to Telegram HTML or plain text. Pure functions, no I/O."""

from __future__ import annotations

import html
import re
from collections.abc import Callable

_FENCE = re.compile(r"^\s*```")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")

_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])|(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])")
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
_SLOT = re.compile("\x00(\\d+)\x00")
_URL = re.compile(r"https?://[^\s<>`]+")
_KEEP = re.compile("\x01(\\d+)\x01")
_TAG = re.compile(r"<(/?)(b|i|code|pre|a)(?:\s[^>]*)?>")


def sanitize_typography(text: str) -> str:
    """Remove em and en dashes: they read as machine-written in chat."""
    text = re.sub(r"(?<=\d)[ \t]*–[ \t]*(?=\d)", "-", text)
    text = re.sub(r"(?m)^([ \t]*)[—–][ \t]*", r"\1", text)
    text = re.sub(r"[ \t]*[—–][ \t]*$", "", text, flags=re.M)
    text = re.sub(r"[ \t]*[—–][ \t]*", ", ", text)
    text = re.sub(r",(?:[ \t]*,)+", ",", text)
    text = re.sub(r"([.!?:;]),", r"\1", text)
    return re.sub(r",[ \t]{2,}", ", ", text)


def sanitize_line(line: str) -> str:
    """Typography fixes (no em or en dashes) that leave inline code spans untouched."""
    spans: list[str] = []

    def hold(m: re.Match[str]) -> str:
        spans.append(m.group(0))
        return f"\x01{len(spans) - 1}\x01"

    out = sanitize_typography(_CODE.sub(hold, line.replace("\x01", "")))
    return _KEEP.sub(lambda m: spans[int(m.group(1))], out)


def _strip_inline(text: str) -> str:
    keep: list[str] = []

    def hold(m: re.Match[str]) -> str:
        keep.append(m.group(0))
        return f"\x01{len(keep) - 1}\x01"

    text = _CODE.sub(r"\1", text.replace("\x01", ""))
    text = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _URL.sub(hold, text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _ITALIC_STAR.sub(r"\1", text)
    text = _ITALIC_UNDER.sub(r"\1", text)
    return _KEEP.sub(lambda m: keep[int(m.group(1))], text)


def _html_inline(text: str) -> str:
    slots: list[str] = []

    def hold(markup: str) -> str:
        slots.append(markup)
        return f"\x00{len(slots) - 1}\x00"

    text = text.replace("\x00", "")
    text = _CODE.sub(lambda m: hold(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = _LINK.sub(
        lambda m: hold(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{_SLOT.sub(lambda s: slots[int(s.group(1))], html.escape(m.group(1), quote=False))}</a>"
        ),
        text,
    )
    text = _URL.sub(lambda m: hold(html.escape(m.group(0), quote=False)), text)
    text = html.escape(text, quote=False)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDER.sub(r"<i>\1</i>", text)
    return _SLOT.sub(lambda m: slots[int(m.group(1))], text)


def _balanced(markup: str) -> bool:
    stack: list[str] = []
    for m in _TAG.finditer(markup):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def _render(md: str, *, as_html: bool) -> str:
    inline: Callable[[str], str] = _html_inline if as_html else _strip_inline
    out: list[str] = []
    code: list[str] | None = None
    for raw in md.replace("\r\n", "\n").split("\n"):
        if code is not None and not _FENCE.match(raw):
            code.append(raw)
            continue
        if _FENCE.match(raw):
            if code is None:
                code = []
            else:
                out.append(_fenced(code, as_html))
                code = None
            continue
        line = sanitize_line(raw)
        if _HR.match(line) or (_TABLE_SEP.match(line) and "|" in line):
            continue
        if m := _HEADING.match(line):
            body = _strip_inline(m.group(1)) if as_html else inline(m.group(1))
            out.append(f"<b>{html.escape(body, quote=False)}</b>" if as_html else body)
        elif _TABLE_ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append(inline(" | ".join(c for c in cells if c)))
        elif m := _BULLET.match(line):
            out.append(f"{m.group(1)}• {inline(m.group(2))}")
        else:
            out.append(inline(line))
    if code is not None:
        out.append(_fenced(code, as_html))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _fenced(lines: list[str], as_html: bool) -> str:
    body = "\n".join(lines)
    return f"<pre>{html.escape(body, quote=False)}</pre>" if as_html else body


def to_plain(markdown: str) -> str:
    return _render(markdown, as_html=False)


def to_telegram_html(markdown: str) -> str:
    rendered = _render(markdown, as_html=True)
    if _balanced(rendered) and "\x00" not in rendered:
        return rendered
    return html.escape(to_plain(markdown).replace("\x00", ""), quote=False)
