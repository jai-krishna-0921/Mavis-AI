"""Markdown outline to Google Slides requests (presentations.batchUpdate).

Rule: each heading line ('# ' to '###### ') starts a slide whose title is the heading; the lines under it
are the slide's body (list markers and emphasis marks are dropped, one paragraph per line, indentation
kept as leading tabs so the layout's bullets nest). Text before the first heading becomes a slide of its
own (first line the title, the rest the body). A slide with no body uses the title-only layout.
Object ids are deterministic (mavis_s<N>, _t, _b) so one batch creates the slide and fills its placeholders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SLIDES = 40
MAX_BODY_CHARS = 2500
MAX_TITLE_CHARS = 200

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")
_LIST = re.compile(r"^(\s*)(?:[-*+]|\d{1,9}[.)])\s+(.*)$")
_MARKS = re.compile(r"(\*\*|__|`|~~)")
_LINK = re.compile(r"\[([^\[\]]*)\]\([^()\s]*\)")


@dataclass(frozen=True)
class Slide:
    title: str
    body: str


def _plain(text: str) -> str:
    return _MARKS.sub("", _LINK.sub(r"\1", text)).strip()


def _line(raw: str) -> str:
    m = _LIST.match(raw)
    if m is None:
        return _plain(raw)
    depth = min(len(m[1].replace("\t", "  ")) // 2, 4)
    return "\t" * depth + _plain(m[2])


def parse_outline(markdown: str) -> list[Slide]:
    slides: list[tuple[str, list[str]]] = []
    preface: list[str] = []
    for raw in markdown.replace("\r\n", "\n").split("\n"):
        h = _HEADING.match(raw)
        if h:
            slides.append((_plain(h[1]), []))
        elif raw.strip():
            (slides[-1][1] if slides else preface).append(_line(raw))
    if preface:
        slides.insert(0, (preface[0].strip(), preface[1:]))
    return [Slide(title[:MAX_TITLE_CHARS], "\n".join(body)[:MAX_BODY_CHARS])
            for title, body in slides[:MAX_SLIDES]]


def slide_requests(slides: list[Slide]) -> list[dict]:
    out: list[dict] = []
    for n, slide in enumerate(slides, 1):
        sid, tid, bid = f"mavis_s{n}", f"mavis_s{n}_t", f"mavis_s{n}_b"
        mappings = [{"layoutPlaceholder": {"type": "TITLE", "index": 0}, "objectId": tid}]
        if slide.body:
            mappings.append({"layoutPlaceholder": {"type": "BODY", "index": 0}, "objectId": bid})
        out.append({"createSlide": {
            "objectId": sid, "insertionIndex": n - 1,
            "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY" if slide.body else "TITLE_ONLY"},
            "placeholderIdMappings": mappings}})
        if slide.title:
            out.append({"insertText": {"objectId": tid, "text": slide.title}})
        if slide.body:
            out.append({"insertText": {"objectId": bid, "text": slide.body}})
    return out
