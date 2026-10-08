"""Markdown (as written by the model) to Telegram HTML or plain text. Pure functions, no I/O."""

from __future__ import annotations

import html
import re
import unicodedata
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


# --- dash normalisation (phase A4, fix round 1) -----------------------------------------------------
#
# A safety net applied once, at the send boundary (the channel renderer); the persona asks for colons
# and "to" instead of dashes. One simple rule, no word lists:
#   - ASCII hyphen-minus (U+002D) is never touched.
#   - Every other Unicode dash (category Pd) is normalised:
#       between two digits with no or thin spacing      -> "-"   (15:00 – 16:00 -> 15:00-16:00)
#       right after a **bold** label at a line/bullet start -> ":"
#       at a line start -> a "- " bullet; at a line end -> dropped
#       with a space on either side elsewhere             -> ", "
#       unspaced elsewhere                                -> "-"
#   - Quoted text ("...", '...', curly quotes), inline code, URLs and verbatim spans are untouched.

# Every Unicode dash punctuation character (category Pd) except U+002D (checked against unicodedata).
DASHES = ("\u058a\u05be\u1400\u1806\u2010\u2011\u2012\u2013\u2014\u2015\u2e17\u2e1a\u2e3a\u2e3b\u2e40"
          "\u2e5d\u301c\u3030\u30a0\ufe31\ufe32\ufe58\ufe63\uff0d\U00010ead")
_PD = f"[{DASHES}]"
_SP = r"[^\S\n]"                 # any horizontal Unicode whitespace (U+00A0, U+202F, ... included)
_THIN = "[\u2009\u200a\u202f]"   # thin, hair and narrow no-break space
_DIGITS = re.compile(rf"(?<=\d)({_THIN}*){_PD}({_THIN}*)(?=\d)")
_BOLD_LABEL = re.compile(
    rf"^({_SP}*(?:[-*+•]{_SP}+|\d+[.)]{_SP}+)?(?:\*\*[^*\n]+\*\*|__[^_\n]+__)){_SP}*{_PD}{_SP}*"
)
_LEADING = re.compile(rf"^({_SP}*){_PD}{_SP}+")
_TRAILING = re.compile(rf"{_SP}*{_PD}{_SP}*$")
_SPACED = re.compile(rf"{_SP}+{_PD}{_SP}*|{_SP}*{_PD}{_SP}+")
_QUOTED = r"\"[^\"\n]*\"|“[^”\n]*”|(?<!\w)'[^'\n]+'(?!\w)|(?<!\w)‘[^’\n]*’(?!\w)"
_HOLD = re.compile(rf"`[^`\n]+`|https?://[^\s<>`]+|\x02\d+\x02|{_QUOTED}")


def _spaced(m: re.Match[str]) -> str:
    before = m.string[:m.start()].rstrip()
    after = m.string[m.end():]
    if after[:1] in (",", ".", ";", ":", "!", "?"):
        return ""  # "wait —, what": the punctuation that follows already separates
    if before[-1:] in (",", ".", ";", ":", "!", "?"):
        return " "  # "end. — next": keep the sentence's own punctuation
    return ", "


def _normalize_plain(line: str) -> str:
    if not any(d in line for d in DASHES):
        return line
    line = _DIGITS.sub("-", line)  # a range: its thin spacing goes too (15:00-16:00)
    line = _BOLD_LABEL.sub(r"\1: ", line, count=1)
    line = _LEADING.sub(r"\1- ", line)
    line = _TRAILING.sub("", line)
    line = _SPACED.sub(_spaced, line)
    return re.sub(_PD, "-", line)


# --- Unicode space and hyphen variants (track 1 T1.4, hotfix4 H6) ---------------------------------
#
# Part of the same one output pass. One general rule, no word lists, by Unicode property:
#   - every space separator (category Zs: no-break, narrow no-break, thin, em, ideographic...) -> " "
#   - hyphens that join words (U+2010 hyphen, U+2011 non-breaking hyphen) -> "-" (U+2212 minus is a
#     sign, not a hyphen: it stays)
#   - invisible break hints (soft hyphen, zero-width space, word joiner, BOM) -> removed
#     (zero-width joiners stay: emoji sequences and some scripts need them)
# Applies to quoted text too (the change is invisible there), never to inline code, URLs or verbatim spans.
_ZS = "".join(c for c in map(chr, range(0x110000)) if unicodedata.category(c) == "Zs" and c != " ")
_VARIANTS = str.maketrans({**dict.fromkeys(_ZS, " "), "\u2010": "-", "\u2011": "-",
                           "\u00ad": None, "\u200b": None, "\u2060": None, "\ufeff": None})
_VARIANT_CHARS = frozenset(_ZS) | {"\u2010", "\u2011", "\u00ad", "\u200b", "\u2060", "\ufeff"}
_VARIANT_HOLD = re.compile(r"`[^`\n]+`|https?://[^\s<>`]+|\x02\d+\x02")


def normalize_variants(line: str) -> str:
    """Unicode space and hyphen variants -> plain ASCII space and hyphen (code, URLs and held spans kept)."""
    if not any(c in _VARIANT_CHARS for c in line):
        return line
    out, last = [], 0
    for m in _VARIANT_HOLD.finditer(line):
        out.append(line[last:m.start()].translate(_VARIANTS))
        out.append(m.group(0))
        last = m.end()
    out.append(line[last:].translate(_VARIANTS))
    return "".join(out)


def normalize_line(line: str) -> str:
    """Dash and Unicode-variant normalisation for one line; quoted text (dashes only), inline code, URLs
    and held spans are untouched."""
    held: list[str] = []

    def hold(m: re.Match[str]) -> str:
        held.append(m.group(0))
        return f"\x01{len(held) - 1}\x01"

    out = _normalize_plain(_HOLD.sub(hold, line.replace("\x01", "")))
    # after the dash rules, which read thin and narrow spaces around a dash between digits as no space
    return normalize_variants(_KEEP.sub(lambda m: held[int(m.group(1))], out))


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
