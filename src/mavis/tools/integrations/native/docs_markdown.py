"""Markdown to Google Docs API requests (documents.batchUpdate).

The body is inserted as plain text in one insertText at index 1, then styled by ranges:
headings (updateParagraphStyle), bold, italic, strikethrough, inline code, links (updateTextStyle) and
bullets or numbered lists (createParagraphBullets). Nesting is carried by leading tabs, which Docs removes
when it makes the bullets, so every bullet request runs last and from the end of the document backwards:
the tabs it removes then never shift a range that is still to be applied.

Docs indices count UTF-16 code units. Raw HTML and images are kept as literal text. A link whose scheme is
not http, https or mailto is shown as "label (url)" and is not made a link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_MARKDOWN_CHARS = 100_000
MAX_LIST_DEPTH = 8
LINK_SCHEMES = ("http://", "https://", "mailto:")
MONO = {"weightedFontFamily": {"fontFamily": "Courier New"}}
QUOTE_INDENT_PT = 36

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)\d{1,9}[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_INLINE = re.compile(
    r"""(?P<esc>\\[\\`*_{}\[\]()#+\-.!~|>])
    |`(?P<code>[^`\n]+)`
    |\[(?P<label>[^\]\n]+)\]\((?P<url>[^)\s]+)(?:\s+"[^"]*")?\)
    |\*\*\*(?P<bi>[^\n]+?)\*\*\*
    |\*\*(?P<b1>[^\n]+?)\*\*
    |(?<!\w)__(?P<b2>[^\n]+?)__(?!\w)
    |~~(?P<s>[^\n]+?)~~
    |\*(?!\s)(?P<i1>[^\n*]+?)(?<!\s)\*
    |(?<!\w)_(?!\s)(?P<i2>[^\n_]+?)(?<!\s)_(?!\w)""",
    re.VERBOSE,
)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


Span = tuple[int, int, dict, str]  # (start, end) in characters of the paragraph text; style; fields mask


def parse_inline(src: str) -> tuple[str, list[Span]]:
    """(plain text, style spans) for one line of inline Markdown. Markers with no closing partner stay."""
    out: list[str] = []
    spans: list[Span] = []
    size, pos = 0, 0

    def add(text: str) -> None:
        nonlocal size
        out.append(text)
        size += len(text)

    def wrap(inner: str, style: dict, fields: str) -> None:
        nonlocal size
        start = size
        plain, inner_spans = parse_inline(inner)
        add(plain)
        spans.extend((s + start, e + start, st, f) for s, e, st, f in inner_spans)
        spans.append((start, size, style, fields))

    while pos < len(src):
        m = _INLINE.search(src, pos)
        if m is None:
            add(src[pos:])
            break
        add(src[pos:m.start()])
        pos = m.end()
        if m["esc"]:
            add(m["esc"][1])
        elif m["code"]:
            start = size
            add(m["code"])
            spans.append((start, size, MONO, "weightedFontFamily"))
        elif m["label"]:
            url = m["url"]
            if url.lower().startswith(LINK_SCHEMES):
                wrap(m["label"], {"link": {"url": url}}, "link")
            else:
                add(f"{m['label']} ({url})")
        elif m["bi"]:
            wrap(m["bi"], {"bold": True, "italic": True}, "bold,italic")
        elif m["b1"] or m["b2"]:
            wrap(m["b1"] or m["b2"], {"bold": True}, "bold")
        elif m["s"]:
            wrap(m["s"], {"strikethrough": True}, "strikethrough")
        else:  # the italic branch: i1 or i2
            wrap(m["i1"] or m["i2"], {"italic": True}, "italic")
    return "".join(out), spans


@dataclass
class _Para:
    kind: str  # "p", "h1".."h6", "ul", "ol", "code", "quote", "blank"
    text: str = ""
    spans: list[Span] = field(default_factory=list)
    level: int = 0


def _level(indent: str) -> int:
    return min(indent.count("\t") + len(indent.replace("\t", "")) // 2, MAX_LIST_DEPTH)


def _cells(row: str) -> str:
    return " | ".join(c.strip() for c in row.strip().strip("|").split("|"))


def _paragraphs(markdown: str) -> list[_Para]:
    paras: list[_Para] = []
    fence = ""
    pending_blank = False

    def push(p: _Para) -> None:
        nonlocal pending_blank
        if pending_blank and p.kind == "p" and paras and paras[-1].kind == "p":
            paras.append(_Para("p"))
        pending_blank = False
        paras.append(p)

    for raw in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        fence_match = _FENCE.match(line)
        if fence:
            if fence_match and line.strip().startswith(fence):
                fence = ""
            else:
                push(_Para("code", line.replace("\t", "    ")))
            continue
        if fence_match:
            fence = fence_match[1]
            continue
        if not line.strip():
            pending_blank = True
            continue
        if _RULE.match(line) or ("|" in line and _TABLE_SEPARATOR.match(line)):
            continue
        m = _HEADING.match(line)
        if m:
            plain, spans = parse_inline(m[2])
            push(_Para(f"h{len(m[1])}", plain, spans))
            continue
        m, kind = _BULLET.match(line), "ul"
        if m is None:
            m, kind = _NUMBER.match(line), "ol"
        if m:
            plain, spans = parse_inline(m[2])
            push(_Para(kind, plain, spans, _level(m[1])))
            continue
        m = _QUOTE.match(line)
        if m:
            plain, spans = parse_inline(m[1])
            push(_Para("quote", plain, spans))
            continue
        if _TABLE_ROW.match(line):
            line = _cells(line)
        plain, spans = parse_inline(line.strip())
        push(_Para("p", plain, spans))
    return paras


PRESETS = {"ul": "BULLET_DISC_CIRCLE_SQUARE", "ol": "NUMBERED_DECIMAL_ALPHA_ROMAN"}


def markdown_requests(markdown: str) -> list[dict]:
    """documents.batchUpdate requests that write `markdown` into an empty document. [] for blank input.
    The caller has checked MAX_MARKDOWN_CHARS."""
    paras = _paragraphs(markdown)
    if not paras:
        return []
    lines: list[str] = []
    styles: list[dict] = []
    groups: list[tuple[str, int, int]] = []  # (preset, start, end) of a run of list paragraphs
    cursor = 1
    for p in paras:
        tabs = "\t" * p.level if p.kind in PRESETS else ""
        lines.append(tabs + p.text)
        start = cursor + len(tabs)
        end = cursor + utf16_len(tabs + p.text) + 1  # + the paragraph's newline
        for s, e, style, mask in p.spans:
            first, last = start + utf16_len(p.text[:s]), start + utf16_len(p.text[:e])
            styles.append(_text_style(first, last, style, mask))
        if p.kind.startswith("h"):
            heading = {"namedStyleType": f"HEADING_{p.kind[1]}"}
            styles.append(_para_style(cursor, end, heading, "namedStyleType"))
        elif p.kind == "code" and p.text:
            styles.append(_text_style(start, end - 1, MONO, "weightedFontFamily"))
        elif p.kind == "quote":
            indent = {"indentStart": {"magnitude": QUOTE_INDENT_PT, "unit": "PT"}}
            styles.append(_para_style(cursor, end, indent, "indentStart"))
        elif p.kind in PRESETS:
            if groups and groups[-1][0] == PRESETS[p.kind] and groups[-1][2] == cursor:
                groups[-1] = (groups[-1][0], groups[-1][1], end)
            else:
                groups.append((PRESETS[p.kind], cursor, end))
        cursor = end
    body = "\n".join(lines)  # the document's own final newline closes the last paragraph
    bullets = [
        {"createParagraphBullets": {"range": {"startIndex": s, "endIndex": e}, "bulletPreset": preset}}
        for preset, s, e in reversed(groups)
    ]
    return [{"insertText": {"location": {"index": 1}, "text": body}}, *styles, *bullets]


def _range(start: int, end: int) -> dict:
    return {"startIndex": start, "endIndex": end}


def _text_style(start: int, end: int, style: dict, mask: str) -> dict:
    return {"updateTextStyle": {"range": _range(start, end), "textStyle": style, "fields": mask}}


def _para_style(start: int, end: int, style: dict, mask: str) -> dict:
    return {"updateParagraphStyle": {"range": _range(start, end), "paragraphStyle": style, "fields": mask}}
