# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""HTML mail body to text for Outlook: one linear pass, bounded input and depth, hidden content dropped.

Text a reader cannot see but a model can read is the injection channel this guards. An element is dropped
(with everything inside it) when its inline style, its class or id as named by a `<style>` block, or its
attributes hide it: display none, visibility hidden, opacity near 0, tiny font, zero width or height,
off-screen offsets, clipping, transparent text, or text colour that matches (or, with no background set,
is) white. Rules inside `@media` blocks are not evaluated (they would hide the real desktop body).
When unsure the content is dropped: noembed and noframes are hidden, attribute text on end tags is
discarded, and an end tag that crosses open elements around a hidden one (which a browser would ignore or
repair by re-opening the hidden element) hides the rest of the enclosing element.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from mavis.tools.integrations.native import gmail_mime

MAX_HTML = gmail_mime.BODY_CAP * 20  # characters of markup parsed
MAX_DEPTH = 100  # deeper nesting ends the parse: what follows is dropped

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                   "source", "track", "wbr"})
_SKIP = frozenset({"script", "style", "head", "title", "template", "noscript", "iframe", "object", "svg",
                   "noembed", "noframes", "math"})
# Formatting elements a browser re-opens after a block end tag (the adoption agency): only a hidden one of
# these, crossed by a block closer, leaves the extent of the hidden part in doubt.
_FORMATTING = frozenset({"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong",
                         "tt", "u"})
_LIST_ITEMS = {"li": frozenset({"li"}), "dd": frozenset({"dd", "dt"}), "dt": frozenset({"dd", "dt"})}
_CELLS = frozenset({"td", "th"})
_ROWS = frozenset({"tr"})
_SECTIONS = frozenset({"tbody", "thead", "tfoot"})
_ROW_CONTEXT = frozenset({"tr", "tbody", "thead", "tfoot", "table"})
_SELECT = frozenset({"select"})
_OPTION = frozenset({"option"})
_BUTTON = frozenset({"button"})
_PARA = frozenset({"p"})
_TABLE = frozenset({"table"})
_STRUCTURE = frozenset({"table", "tbody", "thead", "tfoot", "tr", "td", "th", "caption"})
_CAPTION = frozenset({"caption"})
_CAPTION_OR_TABLE = frozenset({"caption", "table"})
_TABLE_OR_CELL = frozenset({"table", "td", "th"})
_CLOSES_P = frozenset({
    "address", "article", "aside", "blockquote", "center", "details", "dialog", "dir", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "header", "hgroup", "main", "menu", "nav", "ol", "p",
    "section", "summary", "ul", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "listing", "form", "li", "dd",
    "dt", "plaintext", "table", "hr", "xmp"})
_INTEGRATION = frozenset({"mi", "mo", "mn", "ms", "mtext", "annotation-xml", "foreignobject", "desc"})
_IN_SELECT = frozenset({"option", "optgroup", "select", "script", "template", "hr"})
# End tags a browser treats as block closers when deciding what they close (table parts, list items ...).
_CLOSERS = gmail_mime._BLOCK_TAGS | frozenset({
    "td", "th", "dd", "dt", "dl", "tbody", "thead", "tfoot", "caption", "form", "nav", "aside", "main",
    "figure", "fieldset", "address", "center", "button", "object", "applet", "marquee", "select"})
# Elements that stop a browser's search for the element an end tag closes: crossing one means it is ignored.
_SCOPE = frozenset({"table", "td", "th", "caption", "object", "template", "applet", "marquee", "select",
                    "mi", "mo", "mn", "ms", "mtext", "annotation-xml", "foreignobject", "desc"})
_TABLE_SCOPE = frozenset({"table", "template"})  # end tags of table parts look no further than the table
_SCOPE_EXTRA = {"li": frozenset({"ul", "ol"}), "p": frozenset({"button"})}
# HTML tags that leave svg/math: their "/" is ignored there, so they stay open (hidden or not).
_BREAKOUT = frozenset({"b", "big", "blockquote", "body", "center", "code", "dd", "div", "dl", "dt", "em",
                       "h1", "h2", "h3", "h4", "h5", "h6", "head", "i", "li", "listing", "menu", "nobr", "ol",
                       "p", "pre", "ruby", "s", "small", "span", "strong", "strike", "sub", "sup", "table",
                       "tt", "u", "ul", "var", "font"})
_FOREIGN = frozenset({"svg", "math"})  # self-closing children there really are empty elements
# Elements a browser will not close implicitly: an end tag that would cross one is ignored (or, for
# formatting elements, handled by the adoption agency) rather than closing it.
_SPECIAL = _CLOSERS | _SKIP | frozenset({"button", "select", "applet", "marquee", "object", "body", "html",
                                         "form", "iframe", "option", "optgroup", "colgroup", "col"})
_P_STOPS = frozenset({"button", "table", "td", "th", "caption", "object", "marquee", "applet", "template"})
_TABLE_PARTS = frozenset({"tr", "td", "th", "tbody", "thead", "tfoot", "caption", "colgroup", "col"})
_BLOCK = gmail_mime._BLOCK_TAGS

_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)", re.S)
_BRACES = re.compile(r"[{}]")
_ASCII_LETTER = re.compile(r"[A-Za-z]")
_TAG_NAME = re.compile(r"<([A-Za-z][^\s/>]*)")
_END_NAME = re.compile(r"</([A-Za-z][^\s/>]*)")
_RAW_TEXT = {"script": re.compile(r"</script", re.I), "style": re.compile(r"</style", re.I)}
# Raw-text and RCDATA elements HTMLParser does not know: their content is text up to the closer, never markup.
_ESCAPED_RAW = {name: re.compile(rf"</{name}(?=[\s/>])", re.I)
                for name in ("textarea", "xmp", "noembed", "noframes", "title", "iframe")}
_PLAINTEXT = "plaintext"  # swallows the rest of the document
_STYLE_OPEN = re.compile(r"<style", re.I)
_STYLE_CLOSE = re.compile(r"</style", re.I)
_NUMBER = re.compile(r"^\s*(-?(?:\d+\.?\d*|\.\d+))\s*([a-z%]*)\s*$")
_COLOR_TOKEN = re.compile(r"#[0-9a-f]{3,8}\b|(?:rgb|hsl)a?\([^)]*\)|\b[a-z]+\b", re.I)
_NAMED = {"white": (255, 255, 255), "ivory": (255, 255, 240), "snow": (255, 250, 250),
          "whitesmoke": (245, 245, 245), "black": (0, 0, 0), "transparent": "t"}
_UNITS_PX = {"": 1.0, "px": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0, "%": 0.16}
OFFSCREEN_PX = -500
MAX_STYLE_TEXT = 50_000  # characters of <style> blocks read in all; later rules are ignored
MAX_DECLARATIONS = 8_000  # one style attribute or rule body longer than this is treated as hiding
Bg = tuple[int, int, int] | str | None  # a background: rgb, "image", or unknown


def _unescape(m: re.Match[str]) -> str:
    if m[1]:
        code = int(m[1], 16)
        return chr(code) if 0 < code < 0x110000 else ""
    return m[2] or ""


def strip_comments(text: str) -> str:
    """Remove /* ... */ comments in one pass (an unclosed one runs to the end)."""
    out: list[str] = []
    pos = 0
    while (i := text.find("/*", pos)) != -1:
        out.append(text[pos:i])
        j = text.find("*/", i + 2)
        if j == -1:
            return "".join(out)
        pos = j + 2
    out.append(text[pos:])
    return "".join(out)


def declarations(css: str) -> dict[str, str]:
    """name -> lowercase value of an inline style, with comments removed and CSS escapes decoded. A style
    too long to inspect cheaply is treated as hiding."""
    if len(css) > MAX_DECLARATIONS:
        return {"display": "none"}
    css = _CSS_ESCAPE.sub(_unescape, strip_comments(css))
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
    for tok in _COLOR_TOKEN.findall(value[:100]):  # bounded: a pathological value must not cost much
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


def _style_texts(markup: str) -> list[str]:
    """The text of each closed <style> block, found with linear scans, at most MAX_STYLE_TEXT in all."""
    out: list[str] = []
    total = pos = 0
    while total < MAX_STYLE_TEXT and (m := _STYLE_OPEN.search(markup, pos)):
        start = markup.find(">", m.end())
        if start == -1 or not (end := _STYLE_CLOSE.search(markup, start)):
            break
        out.append(markup[start + 1:end.start()][:MAX_STYLE_TEXT - total])
        total += end.start() - start
        pos = end.end()
    return out


def hiding_rules(markup: str) -> tuple[set[str], set[str], set[str]]:
    """Classes, ids and bare tag names that a `<style>` block hides (colour-only rules do not count).
    One linear scan over the braces; at-rules (`@media ...`) are skipped whole."""
    classes: set[str] = set()
    ids: set[str] = set()
    tags: set[str] = set()
    for block in _style_texts(markup):
        css = strip_comments(block)
        depth = last = body_start = 0
        prelude = ""
        for m in _BRACES.finditer(css):
            if m[0] == "{":
                if depth == 0:
                    prelude, body_start = css[last:m.start()].strip().lower(), m.end()
                depth += 1
            elif depth == 0:
                last = m.end()
            else:
                depth -= 1
                if depth:
                    continue
                last = m.end()
                if prelude.startswith("@"):
                    continue
                if not style_hides(declarations(css[body_start:m.start()]), "image"):
                    continue
                for one in prelude.split(","):
                    one = one.strip()
                    compound = re.split(r"[\s>+~]+", one)[-1]
                    classes.update(re.findall(r"\.([\w-]+)", compound))
                    ids.update(re.findall(r"#([\w-]+)", compound))
                    if compound == one and re.fullmatch(r"[a-z][\w-]*", compound):
                        tags.add(compound)
    return classes, ids, tags


class _Extractor(HTMLParser):
    def __init__(self, limit: int, rules: tuple[set[str], set[str], set[str]], styles: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self._styles = styles
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
        if not self._styles:  # tag-only mode (Gmail): script, style, noscript, noembed ... are hidden
            return tag in _SKIP, None
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
        foreign = self._foreign()
        if tag in _BREAKOUT and foreign:
            self._close_from(self._outermost_foreign())  # leaves svg/math; the hidden stays as ghosts
            foreign = False
        if foreign:  # an svg or math element: none of the HTML rules below apply to it
            hide, bg = self._hides(tag, attrs, self._stack[-1][2] if self._stack else None)
            self._stack.append((tag, hide, bg))
            self._open[tag] = self._open.get(tag, 0) + 1
            if hide:
                self._hidden += 1
            return
        if tag == "select" and self._open.get("select"):  # acts as </select> and is itself dropped
            self._close_from(self._find(_SELECT, frozenset()))
            return
        if tag in ("select", "template"):
            self._doubt()  # their parsing rules (and what they swallow) are not followed: hide the rest
        in_select = self._open.get("select") and tag not in _IN_SELECT
        if not in_select:
            self._close_implied(tag)
        if len(self._stack) >= MAX_DEPTH:
            self.cut = True  # absurd nesting: drop the rest rather than walk it
            return
        hide, bg = self._hides(tag, attrs, self._stack[-1][2] if self._stack else None)
        no_table = tag in _TABLE_PARTS and not self._open.get("table")
        if in_select or no_table:  # a browser ignores these start tags here; a hidden one stays as a ghost
            if hide:
                self._ghost((tag, True, bg))
                self._hidden += 1
            return
        if tag in ("td", "th") and self._nearest(_ROW_CONTEXT) != "tr":
            self._stack.append(("tr", False, bg))  # the row a browser implies
            self._open["tr"] = self._open.get("tr", 0) + 1
        self._stack.append((tag, hide, bg))
        self._open[tag] = self._open.get(tag, 0) + 1
        if hide:
            self._hidden += 1
        elif tag in _BLOCK:
            self._emit("\n- " if tag == "li" else "\n")

    def _foreign(self) -> bool:
        """Is the insertion point inside svg or math content (and not in an HTML integration point)?"""
        if not (self._open.get("svg") or self._open.get("math")):
            return False
        for frame in reversed(self._stack):
            if frame[0] in _INTEGRATION:
                return False
            if frame[0] in _FOREIGN:
                return True
        return False

    def _outermost_foreign(self) -> int:
        for idx, frame in enumerate(self._stack):
            if frame[0] in _FOREIGN:
                return idx
        return len(self._stack)

    def _nearest(self, names: frozenset[str]) -> str:
        """Name of the nearest open element in `names` (the stack is walked from the top)."""
        for frame in reversed(self._stack):
            if frame[0] in names:
                return frame[0]
        return ""

    def _ghost(self, frame: tuple[str, bool, Bg]) -> None:
        """A hidden element a browser has already closed. It stays on the stack, unmatchable, so what follows
        keeps being hidden (when in doubt hide) and a later end tag for it finds nothing, as in a browser."""
        self._stack.append(("#" + frame[0], True, frame[2]))

    def _close_from(self, idx: int) -> None:
        """Close the elements from stack index `idx` up, as a browser does implicitly. Visible ones
        are dropped, so unclosed rows, items and paragraphs do not stack up as depth; hidden ones become
        ghosts."""
        popped = self._stack[idx:]
        del self._stack[idx:]
        for name, hid, _ in popped:
            if name[0] != "#":
                self._open[name] -= 1
            if hid:
                self._hidden -= 1
                if name in _FORMATTING:  # still in the browser's list of formatting elements: re-opened later
                    self._doubt()
        for name, hid, bg in popped:
            if hid:
                self._stack.append((name if name[0] == "#" else "#" + name, True, bg))
                self._hidden += 1

    def _find(self, targets: frozenset[str], stops: frozenset[str]) -> int:
        """Stack index of the nearest element in `targets` reached before one in `stops`, else -1."""
        for idx in range(len(self._stack) - 1, -1, -1):
            name = self._stack[idx][0]
            if name in targets:
                return idx
            if name in stops:
                return -1
        return -1

    def _close_implied(self, tag: str) -> None:
        """What a browser closes by itself when `tag` starts: an open li, dd, dt, cell, row, option or button,
        then an open p (for a block start). A select start inside a select only closes it (see
        handle_starttag)."""
        idx = -1
        if tag in _TABLE_PARTS:  # "clear the stack back to a table context": foster-parented junk is closed
            for pos in range(len(self._stack) - 1, -1, -1):
                if self._stack[pos][0] in _STRUCTURE:
                    if self._stack[pos][0] not in _CELLS and pos < len(self._stack) - 1:
                        self._close_from(pos + 1)
                    break
        if tag in _TABLE_PARTS and self._nearest(_CAPTION_OR_TABLE) == "caption":
            self._close_from(self._find(_CAPTION, frozenset()))  # acts as </caption>, then goes on below
        if tag in _LIST_ITEMS:
            idx = self._find(_LIST_ITEMS[tag], _SPECIAL - frozenset({"div", "p", "address"}))
        elif tag in ("td", "th"):
            idx = self._find(_CELLS, _ROW_CONTEXT)
        elif tag == "tr":
            idx = self._find(_ROWS, _SECTIONS | frozenset({"table"}))
        elif tag in _SECTIONS or tag in ("caption", "colgroup"):
            idx = self._find(_TABLE, frozenset()) + 1 if self._open.get("table") else -1
            idx = idx if idx > 0 else -1
        elif tag == "table" and self._nearest(_TABLE_OR_CELL) == "table":
            idx = self._find(_TABLE, frozenset())
        elif tag in ("option", "optgroup"):
            idx = self._find(_OPTION, _SELECT)
        elif tag == "button" and self._open.get("button"):
            idx = self._find(_BUTTON, _SCOPE)
        if idx >= 0:
            self._close_from(idx)
        if tag in _CLOSES_P and self._open.get("p"):
            idx = self._find(_PARA, _P_STOPS)
            if idx >= 0:
                self._close_from(idx)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if (tag in _FOREIGN or self._foreign()) and tag not in _BREAKOUT:
            return  # `<path/>` in svg is an empty element: it never stays open
        self.handle_starttag(tag, attrs)  # HTML5 ignores the "/" on a non-void element: `<div/>` opens a div

    def _doubt(self) -> None:
        """The extent of a hidden element can no longer be told: everything after is hidden."""
        self._hidden += 1

    def _drop(self, idx: int) -> None:
        name, hid, _ = self._stack.pop(idx)
        if name[0] != "#":
            self._open[name] -= 1
        if hid:
            self._hidden -= 1

    def handle_endtag(self, tag: str) -> None:
        """Follow what a browser does with this end tag, failing toward hiding. A browser ignores an end tag
        that would cross a scope boundary or (for "any other end tag") a special element, so the hidden
        element stays open; it removes a crossed formatting element without closing the blocks around it
        and re-opens a hidden one on the next text. Where that cannot be followed, the rest is hidden."""
        if not self._open.get(tag):
            return
        stack = self._stack
        idx = len(stack) - 1
        while stack[idx][0] != tag:  # at most MAX_DEPTH frames
            idx -= 1
        crossed = stack[idx + 1:]
        special = any(f[0] in _SPECIAL for f in crossed)
        reopened = any(f[1] and f[0] in _FORMATTING for f in crossed)  # reconstructed on the next text
        if tag in _FORMATTING or tag == "form":
            if special or (tag == "form" and crossed):
                # adoption agency / form pointer: only this element leaves the stack
                hid = stack[idx][1]
                self._drop(idx)
                if reopened or hid:
                    self._doubt()
                return
        elif tag in _CLOSERS:
            stops = _SCOPE | _SCOPE_EXTRA.get(tag, frozenset())
            if tag in _TABLE_PARTS or tag == "table":
                stops = _TABLE_SCOPE
            if not (self._open.get("math") or self._open.get("svg")):
                stops = stops - _INTEGRATION  # mtext, desc ... are ordinary elements outside svg and math
            if any(f[0] in stops for f in crossed):
                return  # out of scope: ignored, whatever is hidden stays open
        elif special:
            return  # "any other end tag" stops at a special element and is ignored
        while len(stack) > idx:  # every element is pushed and popped once: linear overall
            self._drop(len(stack) - 1)
        if reopened:
            self._doubt()
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        self._emit(data)


def _tag_end(markup: str, i: int) -> int:
    """Index of the `>` closing the tag that starts at `i`, honouring quoted attribute values (a quote opens
    a value only right after `=`). A `<` is an ordinary character inside a tag, as in a browser. -1: the
    input ended first."""
    quote = ""
    prev = ""
    for j in range(i + 1, len(markup)):
        ch = markup[j]
        if quote:
            if ch == quote:
                quote = ""
                prev = ch
            continue
        if ch == ">":
            return j
        if ch in "\"'" and prev == "=":
            quote = ch
        if not ch.isspace():
            prev = ch
    return -1


def defang(markup: str) -> str:
    """Make the markup safe for HTMLParser, which rescans the rest of its input for every unclosed construct
    (`<a <a <a ...`, `<!--` repeated) and so goes quadratic. One linear pass, failing toward hiding:
    comments, declarations, `<![if x]>` sections and processing instructions are dropped; a tag whose `>` is
    missing drops the rest of the markup (at most once, so the pass stays linear); a `<` that cannot start a
    tag becomes literal text. A `<` inside a tag belongs to it, and complete tags are passed through exactly
    as they were. The text of textarea, xmp, noembed, noframes and title (up to their closer) and of
    plaintext (to the end) is escaped so it can only be text."""
    out: list[str] = []
    pos = 0
    while (i := markup.find("<", pos)) != -1:
        out.append(markup[pos:i])
        nxt = markup[i + 1:i + 2]
        if markup.startswith("<!--", i):
            j = markup.find("-->", i + 2)
            if j == -1:
                return "".join(out)
            pos = j + 3
        elif nxt in ("!", "?") or (nxt == "/" and not _ASCII_LETTER.match(markup, i + 2)):
            j = markup.find(">", i + 1)  # a bogus comment, declaration or marked section: never content
            if j == -1:
                return "".join(out)
            pos = j + 1
        elif not (nxt == "/" or _ASCII_LETTER.match(markup, i + 1)):
            out.append("&lt;")  # `1 < 2`: text
            pos = i + 1
        else:
            end = _tag_end(markup, i)
            if end == -1:
                return "".join(out)
            if nxt == "/":  # attribute text on an end tag is never content: keep only the name
                closer = _END_NAME.match(markup, i)
                out.append(f"</{closer[1]}>" if closer else "")
            else:
                out.append(markup[i:end + 1])
            pos = end + 1
            name = _TAG_NAME.match(markup, i)
            tag = name[1].lower() if name and nxt != "/" else ""
            raw = _RAW_TEXT.get(tag)
            if raw is not None and markup[end - 1] != "/":  # script/style: its text is not markup
                m = raw.search(markup, pos)
                if m is None:
                    out.append(markup[pos:])
                    return "".join(out)
                out.append(markup[pos:m.start()])
                pos = m.start()
            elif tag == _PLAINTEXT:
                out.append(markup[pos:].replace("<", "&lt;"))
                return "".join(out)
            elif (raw := _ESCAPED_RAW.get(tag) or raw) is not None:
                m = raw.search(markup, pos)
                stop = m.start() if m else len(markup)
                out.append(markup[pos:stop].replace("<", "&lt;"))
                pos = stop
    out.append(markup[pos:])
    return "".join(out)


def html_to_text_limited(markup: str, limit: int, *, styles: bool = True) -> tuple[str, bool]:
    """(text, cut): `cut` is True when the markup, its nesting or the text exceeded a bound. `styles=False`
    hides by tag only (no style, class or attribute rules): the Gmail reader."""
    cut = len(markup) > MAX_HTML
    markup = defang(markup[:MAX_HTML])
    parser = _Extractor(limit, hiding_rules(markup) if styles else (set(), set(), set()), styles)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup degrades to whatever text was seen
        pass
    return gmail_mime.tidy("".join(parser.parts)), cut or parser.cut


def html_to_text(markup: str, limit: int = MAX_HTML) -> str:
    return html_to_text_limited(markup, limit)[0]
