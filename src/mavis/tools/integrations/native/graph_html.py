# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""HTML mail body to text for Outlook: one linear pass, bounded input and depth, hidden content dropped.

Text a reader cannot see but a model can read is the injection channel this guards. An element is dropped
(with everything inside it) when its inline style, its class or id as named by a `<style>` block, or its
attributes hide it: display none, visibility hidden, opacity near 0, tiny font, zero width or height,
off-screen offsets, clipping, transparent text, or text colour that matches (or, with no background set,
is) white. Rules inside `@media` blocks are not evaluated (they would hide the real desktop body).
When unsure the content is dropped.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from mavis.tools.integrations.native import gmail_mime

MAX_HTML = gmail_mime.BODY_CAP * 20  # characters of markup parsed
MAX_DEPTH = 100  # deeper nesting ends the parse: what follows is dropped

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                   "source", "track", "wbr"})
_SKIP = frozenset({"script", "style", "head", "title", "template", "noscript", "iframe", "object", "svg"})
_BLOCK = gmail_mime._BLOCK_TAGS

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)", re.S)
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_AT_BLOCK = re.compile(r"@[^{};]+\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}")
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_NUMBER = re.compile(r"^\s*(-?(?:\d+\.?\d*|\.\d+))\s*([a-z%]*)\s*$")
_COLOR_TOKEN = re.compile(r"#[0-9a-f]{3,8}\b|(?:rgb|hsl)a?\([^)]*\)|\b[a-z]+\b", re.I)
_NAMED = {"white": (255, 255, 255), "ivory": (255, 255, 240), "snow": (255, 250, 250),
          "whitesmoke": (245, 245, 245), "black": (0, 0, 0), "transparent": "t"}
_UNITS_PX = {"": 1.0, "px": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0, "%": 0.16}
OFFSCREEN_PX = -500
Bg = tuple[int, int, int] | str | None  # a background: rgb, "image", or unknown


def _unescape(m: re.Match[str]) -> str:
    if m[1]:
        code = int(m[1], 16)
        return chr(code) if 0 < code < 0x110000 else ""
    return m[2] or ""


def declarations(css: str) -> dict[str, str]:
    """name -> lowercase value of an inline style, with comments removed and CSS escapes decoded."""
    css = _CSS_ESCAPE.sub(_unescape, _CSS_COMMENT.sub("", css))
    out: dict[str, str] = {}
    for item in css.split(";"):
        name, sep, value = item.partition(":")
        if sep:
            out[name.strip().lower()] = re.sub(r"\s*!important\s*$", "", value.strip(), flags=re.I).lower()
    return out


def _px(value: str) -> float | None:
    m = _NUMBER.match(value)
    if not m or m[2] not in _UNITS_PX:
        return None
    return float(m[1]) * _UNITS_PX[m[2]]


def _color(value: str) -> tuple[int, int, int] | str | None:
    """(r, g, b), "t" for transparent, or None when unknown."""
    for tok in _COLOR_TOKEN.findall(value):
        t = tok.lower()
        if t.startswith("#"):
            h = t[1:]
            if len(h) in (3, 4):
                h = "".join(c * 2 for c in h)
            if len(h) not in (6, 8) or not re.fullmatch(r"[0-9a-f]+", h):
                return None
            if len(h) == 8 and int(h[6:], 16) < 26:
                return "t"
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        if t.startswith(("rgb", "hsl")):
            nums = re.findall(r"-?\d*\.?\d+%?", t[t.index("(") + 1:])
            if len(nums) > 3 and float(nums[3].rstrip("%")) / (100 if nums[3].endswith("%") else 1) < 0.1:
                return "t"
            if t.startswith("rgb") and len(nums) >= 3:
                return tuple(  # type: ignore[return-value]
                    round(float(n.rstrip("%")) * (2.55 if n.endswith("%") else 1)) for n in nums[:3])
            if t.startswith("hsl") and len(nums) >= 3 and nums[2] in ("100%", "99%", "98%"):
                return 255, 255, 255
            return None
        if t in _NAMED:
            return _NAMED[t]
    return None


def _near(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return all(abs(x - y) <= 12 for x, y in zip(a, b, strict=True))


def _background(d: dict[str, str]) -> Bg:
    """A background colour, "image" for an image or gradient, None when none is set (or it is transparent)."""
    value = d.get("background-color") or d.get("background") or ""
    if "url(" in value or "gradient" in value or "url(" in d.get("background-image", ""):
        return "image"
    c = _color(value)
    return None if c == "t" else c


def style_hides(d: dict[str, str], bg: Bg) -> bool:
    """Would a reader not see text styled like this? `bg` is what it sits on (None: assume white)."""
    g = d.get
    if g("display", "") == "none" or g("mso-hide", "") == "all":
        return True
    if g("visibility", "") in ("hidden", "collapse"):
        return True
    if (m := _NUMBER.match(g("opacity", ""))) and float(m[1]) / (100 if m[2] == "%" else 1) < 0.1:
        return True
    if (px := _px(g("font-size", ""))) is not None and px < 4:
        return True
    if any(_px(g(p, "x")) == 0 for p in ("width", "height", "max-width", "max-height", "line-height")):
        return True
    for prop in ("left", "top", "right", "bottom", "text-indent", "margin-left", "margin-top"):
        if (px := _px(g(prop, ""))) is not None and px <= OFFSCREEN_PX:
            return True
    if re.sub(r"\s", "", g("clip", "")).startswith("rect(0") or "inset(100%" in g("clip-path", ""):
        return True
    if re.sub(r"\s", "", g("transform", "")).startswith("scale(0)"):
        return True
    if "color" in d:
        c = _color(g("color", ""))
        if c == "t":
            return True
        if isinstance(c, tuple):
            if bg == "image":
                return False
            return _near(c, (255, 255, 255) if bg is None else bg) if not isinstance(bg, str) else False
    return False


def hiding_rules(markup: str) -> tuple[set[str], set[str], set[str]]:
    """Classes, ids and bare tag names that a `<style>` block hides (colour-only rules do not count)."""
    classes: set[str] = set()
    ids: set[str] = set()
    tags: set[str] = set()
    for block in _STYLE_BLOCK.findall(markup):
        css = _AT_BLOCK.sub("", _CSS_COMMENT.sub("", block))
        for m in _RULE.finditer(css):
            if not style_hides(declarations(m[2]), "image"):
                continue
            for one in m[1].split(","):
                one = one.strip().lower()
                compound = re.split(r"[\s>+~]+", one)[-1]
                classes.update(re.findall(r"\.([\w-]+)", compound))
                ids.update(re.findall(r"#([\w-]+)", compound))
                if compound == one and re.fullmatch(r"[a-z][\w-]*", compound):
                    tags.add(compound)
    return classes, ids, tags


class _Extractor(HTMLParser):
    def __init__(self, limit: int, rules: tuple[set[str], set[str], set[str]]) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.cut = False
        self._size, self._limit = 0, limit
        self._classes, self._ids, self._tags = rules
        self._stack: list[tuple[str, bool, Bg]] = []
        self._open: dict[str, int] = {}
        self._hidden = 0

    def _emit(self, text: str) -> None:
        if self._hidden or self.cut:
            return
        self.parts.append(text)
        self._size += len(text)
        if self._size > self._limit:
            self.cut = True

    def _hides(self, tag: str, attrs: list[tuple[str, str | None]], parent_bg: Bg) -> tuple[bool, Bg]:
        a = {k: (v or "") for k, v in attrs}
        if tag in _SKIP or "hidden" in a or tag in self._tags:
            return True, parent_bg
        if self._ids and a.get("id", "").lower() in self._ids:
            return True, parent_bg
        if self._classes and any(c in self._classes for c in a.get("class", "").lower().split()):
            return True, parent_bg
        d = declarations(a["style"]) if "style" in a else {}
        bg = _background(d)
        if bg is None and a.get("bgcolor"):
            c = _color(a["bgcolor"].lower())
            bg = c if isinstance(c, tuple) else None
        bg = bg if bg is not None else parent_bg
        if tag == "font" and a.get("color") and "color" not in d:
            d["color"] = a["color"].lower()
        return style_hides(d, bg), bg

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.cut:
            return
        if tag in _VOID:
            if tag in ("br", "hr"):
                self._emit("\n")
            return
        if len(self._stack) >= MAX_DEPTH:
            self.cut = True  # absurd nesting: drop the rest rather than walk it
            return
        hide, bg = self._hides(tag, attrs, self._stack[-1][2] if self._stack else None)
        self._stack.append((tag, hide, bg))
        self._open[tag] = self._open.get(tag, 0) + 1
        if hide:
            self._hidden += 1
        elif tag in _BLOCK:
            self._emit("\n- " if tag == "li" else "\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr") or tag in _BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._open.get(tag):
            return
        while self._stack:  # every element is pushed and popped once: linear overall
            name, hid, _ = self._stack.pop()
            self._open[name] -= 1
            if hid:
                self._hidden -= 1
            if name == tag:
                break
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        self._emit(data)


def html_to_text_limited(markup: str, limit: int) -> tuple[str, bool]:
    """(text, cut): `cut` is True when the markup, its nesting or the text exceeded a bound."""
    cut = len(markup) > MAX_HTML
    markup = markup[:MAX_HTML]
    parser = _Extractor(limit, hiding_rules(markup))
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup degrades to whatever text was seen
        pass
    return gmail_mime.tidy("".join(parser.parts)), cut or parser.cut


def html_to_text(markup: str, limit: int = MAX_HTML) -> str:
    return html_to_text_limited(markup, limit)[0]
