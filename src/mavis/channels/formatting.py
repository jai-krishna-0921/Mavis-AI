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
_HELD = re.compile("\x02(\\d+)\x02")


# --- dash normalisation (phase A4) ------------------------------------------------------------------
#
# Applied once, at the send boundary (the channel renderer), never to stored history, tool arguments or
# verbatim spans. A dash is rewritten by what it means in context:
#   "Label — text" / "Label - text" at a line or bullet start  ->  "Label: text"
#   a range of numbers, times, days or months ("15:00–16:00")  ->  "15:00 to 16:00"
#   any other dash between clauses                              ->  ", "

# Every Unicode space separator (category Zs) other than the ASCII space: no-break, narrow no-break,
# thin, em, ideographic... The model emits U+202F around times ("3\u202fPM").
SPACE_SEPARATORS = "\u00a0\u1680" + "".join(chr(c) for c in range(0x2000, 0x200B)) + "\u202f\u205f\u3000"
_TO_SPACE = str.maketrans(dict.fromkeys(SPACE_SEPARATORS, " "))
# Hyphen lookalikes read as a plain hyphen-minus (the figure dash is the phone-number dash).
_TO_HYPHEN = str.maketrans(dict.fromkeys("\u2010\u2011\u2012\u2212\ufe63\uff0d", "-"))
_LONG = "[\u2013\u2014\u2015\ufe58]"  # en, em, horizontal bar, small em
_DAYS = ("monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|"
         "sat|sun")
_MONTHS = ("january|february|march|april|june|july|august|september|october|november|december|jan|feb|"
           "mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec")
_AMPM = r"(?:\s?[ap]\.?m\.?)"
_NUM = r"\d+(?:[:.]\d{2})?" + _AMPM + "?"
_ATOM = rf"(?:{_NUM}|(?:{_DAYS}|{_MONTHS})\.?)"
# unmistakably a time or a day
_STRONG = rf"(?:\d{{1,2}}:\d{{2}}{_AMPM}?|\d{{1,2}}{_AMPM}|(?:{_DAYS}|{_MONTHS})\.?)"
# a range atom is a whole token: not glued to a word, a hyphen chain, a path, or more digits
_EDGE_L, _EDGE_R = r"(?<![\w\-/])(?<!\d[:.])", r"(?![\w\-/]|[:.]\d)"
# en/em dash between any two range atoms, spaced or not
_RANGE_LONG = re.compile(rf"{_EDGE_L}({_ATOM})[ \t]*{_LONG}[ \t]*({_ATOM}){_EDGE_R}", re.IGNORECASE)
# a spaced hyphen between range atoms ("3 PM - 4 PM", "Mon - Fri")
_RANGE_SPACED = re.compile(rf"{_EDGE_L}({_ATOM})[ \t]+-[ \t]+({_ATOM}){_EDGE_R}", re.IGNORECASE)
# an unspaced hyphen: only when a side is unmistakably a time or day, or both are 1-2 digit numbers,
# and never inside a chain like 2026-10-03 or 555-0132
_RANGE_TIGHT = re.compile(
    rf"{_EDGE_L}(?:({_STRONG})-({_ATOM})|({_ATOM})-({_STRONG})|(\d{{1,2}})-(\d{{1,2}})){_EDGE_R}",
    re.IGNORECASE,
)
# "Label — text": an optional indent and bullet, then a short capitalised label (or **bold** one)
_LABEL = re.compile(
    rf"^(?P<lead>[ \t]*(?:[-*+•][ \t]+|\d+[.)][ \t]+)?)"
    rf"(?P<label>(?:\*\*[^*\n]{{1,48}}\*\*|__[^_\n]{{1,48}}__|[A-Z][^\s.!?:;,]*(?:[ \t][^\s.!?:;,]+){{0,4}}))"
    rf"(?:[ \t]*{_LONG}[ \t]*|[ \t]+--?[ \t]+)(?=\S)"
)
_LEADING = re.compile(rf"^([ \t]*){_LONG}[ \t]*")
_TRAILING = re.compile(rf"[ \t]*{_LONG}[ \t]*$")
_CLAUSE = re.compile(rf"[ \t]*{_LONG}[ \t]*|(?<=\S)[ \t]+--?[ \t]+(?=\S)")
_HOLD = re.compile(r"`[^`\n]+`|https?://[^\s<>`]+|\x02\d+\x02")


def _range(m: re.Match[str]) -> str:
    a, b = (g for g in m.groups() if g is not None)
    return f"{a} to {b}"


def _normalize_plain(line: str) -> str:
    line = line.translate(_TO_SPACE).translate(_TO_HYPHEN)
    line = _RANGE_LONG.sub(_range, line)
    line = _RANGE_SPACED.sub(_range, line)
    line = _RANGE_TIGHT.sub(_range, line)
    line = _LABEL.sub(lambda m: f"{m['lead']}{m['label']}: ", line, count=1)
    line = _LEADING.sub(r"\1- ", line)
    line = _TRAILING.sub("", line)
    line = _CLAUSE.sub(", ", line)
    line = re.sub(r",(?:[ \t]*,)+", ",", line)
    line = re.sub(r"([.!?:;]),", r"\1", line)
    return re.sub(r"(\S)[ \t]{2,}", r"\1 ", line)


def normalize_line(line: str) -> str:
    """Context-aware dash normalisation for one line; inline code, URLs and held spans are untouched."""
    held: list[str] = []

    def hold(m: re.Match[str]) -> str:
        held.append(m.group(0))
        return f"\x01{len(held) - 1}\x01"

    out = _normalize_plain(_HOLD.sub(hold, line.replace("\x01", "")))
    return _KEEP.sub(lambda m: held[int(m.group(1))], out)


def normalize_dashes(text: str) -> str:
    """normalize_line over a message, leaving ``` fenced blocks alone."""
    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        out.append(line if fenced else normalize_line(line))
    return "\n".join(out)


# --- verbatim spans ---------------------------------------------------------------------------------
#
# Text that must reach the user exactly as it is (an approval preview, which must equal the action;
# quoted third-party text) is wrapped in these markers by the code that inserts it. The renderer
# escapes it but never rewrites or parses it; history stores the text without the markers.

VERBATIM_OPEN, VERBATIM_CLOSE = "\x0e", "\x0f"
_VERBATIM = re.compile("\x0e(.*?)(?:\x0f|$)", re.DOTALL)


def verbatim(text: str) -> str:
    clean = str(text).replace(VERBATIM_OPEN, "").replace(VERBATIM_CLOSE, "")
    return f"{VERBATIM_OPEN}{clean}{VERBATIM_CLOSE}" if clean else ""


def strip_verbatim(text: str) -> str:
    return text.replace(VERBATIM_OPEN, "").replace(VERBATIM_CLOSE, "")


def _hold_verbatim(md: str) -> tuple[str, list[str]]:
    """Replace each verbatim span with per-line placeholders (line structure is kept), so markdown
    and dash rules never see its text."""
    spans: list[str] = []

    def hold(m: re.Match[str]) -> str:
        parts = []
        for piece in m.group(1).split("\n"):
            spans.append(piece)
            parts.append(f"\x02{len(spans) - 1}\x02" if piece else "")
        return "\n".join(parts)

    held = _VERBATIM.sub(hold, md.replace("\x02", ""))
    return held.replace(VERBATIM_CLOSE, ""), spans


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
    """The send boundary: verbatim spans held aside, dashes normalised once, markdown rendered."""
    inline: Callable[[str], str] = _html_inline if as_html else _strip_inline
    md, spans = _hold_verbatim(md.replace("\r\n", "\n"))
    out: list[str] = []
    code: list[str] | None = None
    for raw in md.split("\n"):
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
        line = normalize_line(raw)
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
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

    def restore(m: re.Match[str]) -> str:
        span = spans[int(m.group(1))]
        return html.escape(span, quote=False) if as_html else span

    return _HELD.sub(restore, text)


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
