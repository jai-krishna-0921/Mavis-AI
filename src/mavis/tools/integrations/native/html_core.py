# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""HTML mail body to text: the parse and the hide policy, run inside an isolated worker process.

This file imports nothing from the `mavis` package (only the standard library and selectolax), because the
worker runs it as a plain script (`python -I html_core.py`): a fresh interpreter then starts in milliseconds
and holds a few tens of megabytes instead of importing the whole application. `graph_html` is the public
face and owns the worker pool; see there for the isolation rules.

Text a reader cannot see but a model can read is the injection channel this guards. The markup is parsed by
a real HTML5 tree builder (Lexbor), so what a browser would nest, close, re-open or ignore is decided by the
parser and not by rules of ours. An element is then dropped (with everything inside it) when its inline
style, its class or id as named by a `<style>` block, or its attributes hide it: display none, visibility
hidden, opacity near 0, tiny font, zero width or height, off-screen offsets, clipping, transparent text, or
text colour that matches (or, with no background set, is) white. Rules inside `@media` blocks are not
evaluated (they would hide the real desktop body). Always dropped by tag: script, style, head, title,
template, noscript, iframe, object, svg, math, noembed and noframes. A dropdown (`select`) shows its option
labels and nothing else. Comments are never content.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

MAX_HTML = 1_000_000  # characters of markup read; more is not parsed at all (the caller fails closed)
MAX_STYLE_TEXT = 250_000  # characters of <style> blocks in all; more fails closed
MAX_RULES = 12_000  # style rules in all; more fails closed
MAX_DECLARATIONS = 8_000  # one style attribute or rule body longer than this is treated as hiding

NOTICE = "[This message's formatting was too complex to read safely.]"


class TooComplex(Exception):
    """The message is beyond a budget: it is replaced by NOTICE and reported as cut."""


_SKIP = frozenset({"script", "style", "head", "title", "template", "noscript", "iframe", "object", "svg",
                   "noembed", "noframes", "math"})
_BLOCK = frozenset({
    "p", "div", "br", "tr", "table", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "pre", "hr", "section", "article", "header", "footer",
})
_LINES = frozenset({"option"})
_DROPDOWN = frozenset({"option", "optgroup"})
_FOREIGN = frozenset({"svg", "math"})
# Elements that end svg or math content in a browser (WHATWG, "in foreign content"): Lexbor mis-handles sup.
_BREAKOUT = frozenset({
    "b", "big", "blockquote", "body", "br", "center", "code", "dd", "div", "dl", "dt", "em", "embed", "h1",
    "h2", "h3", "h4", "h5", "h6", "head", "hr", "i", "img", "li", "listing", "menu", "meta", "nobr", "ol",
    "p", "pre", "ruby", "s", "small", "span", "strong", "strike", "sub", "sup", "table", "tt", "u", "ul",
    "var",
})
# Integration points, where HTML tags are legitimate children: per kind of foreign root.
_INTEGRATION = {"svg": frozenset({"foreignobject", "desc", "title"}),
                "math": frozenset({"mi", "mo", "mn", "ms", "mtext", "annotation-xml"})}

_SPACES = re.compile(r"[ \t\r\f\v ​-‍﻿]+")
_BLANK_LINES = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})(?:\r\n|[ \t\n\r\f])?|\\(.)", re.S)
_BRACES = re.compile(r"[{}]")
_IMPORTANT = re.compile(r"\s*!\s*important\s*$", re.I)
_NUMBER = re.compile(r"^\s*(-?(?:\d+\.?\d*|\.\d+))\s*([a-z%]*)\s*$")
_COLOR_TOKEN = re.compile(r"#[0-9a-f]{3,8}\b|(?:rgb|hsl)a?\([^)]*\)|\b[a-z]+\b", re.I)
_NAMED = {"white": (255, 255, 255), "ivory": (255, 255, 240), "snow": (255, 250, 250),
          "whitesmoke": (245, 245, 245), "black": (0, 0, 0), "transparent": "t"}
_UNITS_PX = {"": 1.0, "px": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0, "%": 0.16, "ex": 8.0, "ch": 8.0,
             "cm": 37.8, "mm": 3.78, "in": 96.0, "pc": 16.0, "q": 0.94, "vw": 10.0, "vh": 10.0,
             "vmin": 10.0, "vmax": 10.0}
OFFSCREEN_PX = -500
Bg = tuple[int, int, int] | str | None  # a background: rgb, "image", or unknown


def tidy(text: str) -> str:
    """Normalise newlines and runs of blanks; drop control characters."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


# --- CSS -----------------------------------------------------------------------------------------------


def _unescape(m: re.Match[str]) -> str:
    if m[1]:
        code = int(m[1], 16)
        return chr(code) if 0 < code < 0x110000 else "\ufffd"
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


_GLOBAL = ("inherit", "initial", "unset", "revert", "revert-layer")
_LENGTH = r"(?:-?(?:\d+\.?\d*|\.\d+)(?:px|pt|em|rem|%|ex|ch|cm|mm|in|pc|q|vw|vh|vmin|vmax)?)"
_DISPLAY = frozenset({
    "none", "block", "inline", "inline-block", "flex", "inline-flex", "grid", "inline-grid", "table",
    "inline-table", "table-row", "table-cell", "table-row-group", "table-header-group",
    "table-footer-group", "table-column", "table-column-group", "table-caption", "list-item", "contents",
    "flow", "flow-root", "ruby", "ruby-base", "ruby-text", "ruby-base-container", "ruby-text-container",
    *_GLOBAL})


def _keywords(*words: str) -> re.Pattern[str]:
    return re.compile("^(?:" + "|".join(re.escape(w) for w in (*words, *_GLOBAL)) + f"|{_LENGTH})$")


_FONT_SIZE = _keywords("xx-small", "x-small", "small", "medium", "large", "x-large", "xx-large",
                       "xxx-large", "smaller", "larger", "math")
_SIZE = _keywords("auto", "min-content", "max-content", "fit-content", "stretch")
_MAX_SIZE = _keywords("none", "min-content", "max-content", "fit-content", "stretch")
_LINE_HEIGHT = _keywords("normal")
_OFFSET = _keywords("auto")
_INDENT = re.compile(rf"^(?:{_LENGTH}(?: (?:each-line|hanging))*|(?:each-line|hanging)(?: {_LENGTH})?"
                     rf"(?: (?:each-line|hanging))?|{'|'.join(_GLOBAL)})$")
_OPACITY = re.compile(r"^(?:-?(?:\d+\.?\d*|\.\d+)%?|" + "|".join(_GLOBAL) + ")$")
_COLOR_VALUE = re.compile(r"^(?:#[0-9a-f]{3,8}|(?:rgb|hsl)a?\([^()]*\)|[a-z]+)$")
_FUNCTIONS = re.compile(r"^(?:none|auto|(?:[a-z-]+\(.*\)\s*)+(?:[a-z-]+)?|" + "|".join(_GLOBAL) + ")$")


def _valid(name: str, value: str) -> bool:
    """Would a browser accept `value` for `name`, for the properties the hide policy reads (strict, by
    whitelist: a value this does not recognise is ignored, so an earlier declaration keeps applying)?"""
    if name == "display":
        return bool(value) and all(w in _DISPLAY for w in value.split()) and len(value.split()) <= 3
    if name == "visibility":
        return value in ("visible", "hidden", "collapse", *_GLOBAL)
    if name == "opacity":
        return bool(_OPACITY.match(value))
    if name == "mso-hide":
        return bool(re.fullmatch(r"[a-z-]+", value))
    if name == "font-size":
        return bool(_FONT_SIZE.match(value))
    if name in ("width", "height"):
        return bool(_SIZE.match(value))
    if name in ("max-width", "max-height"):
        return bool(_MAX_SIZE.match(value))
    if name == "line-height":
        return bool(_LINE_HEIGHT.match(value))
    if name in ("left", "top", "right", "bottom", "margin-left", "margin-top"):
        return bool(_OFFSET.match(value))
    if name == "text-indent":
        return bool(_INDENT.match(value))
    if name in ("clip", "clip-path", "transform"):
        return bool(_FUNCTIONS.match(value))
    if name == "color":
        return bool(_COLOR_VALUE.match(value)) or value in _GLOBAL
    if name in ("background", "background-color", "background-image"):
        return "var(" not in value and "env(" not in value
    return True


_READ = frozenset({"display", "visibility", "opacity", "mso-hide", "font-size", "width", "height",
                   "max-width", "max-height", "line-height", "left", "top", "right", "bottom",
                   "margin-left", "margin-top", "text-indent", "clip", "clip-path", "transform", "color",
                   "background", "background-color", "background-image"})


def declarations(css: str) -> dict[str, str]:
    """name -> lowercase value of one declaration block, as a browser's cascade resolves it for the
    properties the hide policy reads: the winner is the last valid `!important` declaration, else the last
    valid one; a value the whitelist does not recognise is ignored. A value with a CSS escape is decoded
    exactly (no trimming of an escaped blank) and then judged the same way, but one that decodes to a hiding
    value is trusted as valid (a hex escape of `none` hides in a browser). A block too long to inspect
    cheaply is treated as hiding."""
    if len(css) > MAX_DECLARATIONS:
        return {"display": "none"}
    css = strip_comments(css)
    plain: dict[str, str] = {}
    last_important: dict[str, str] = {}
    for item in css.split(";"):
        name, sep, value = item.partition(":")
        if not sep:
            continue
        name = _CSS_ESCAPE.sub(_unescape, name).strip().lower()
        value = value.strip()
        important = bool(_IMPORTANT.search(value))
        if important:
            value = _IMPORTANT.sub("", value)
        escaped = "\\" in value
        if escaped:
            value = _CSS_ESCAPE.sub(_unescape, value)  # no strip: an escaped blank is part of the value
        value = value.lower()
        if name in _READ:
            if not _valid(name, value) or value != value.strip():
                continue
            if escaped and not style_hides({name: value}, None):
                continue  # an escaped value that does not hide is ignored: fail toward hiding
        (last_important if important else plain)[name] = value
    return {**plain, **last_important}


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


def hiding_rules(blocks: list[str]) -> tuple[set[str], set[str], set[str]]:
    """Classes, ids and bare tag names that a `<style>` block hides (colour-only rules do not count).
    `blocks` are the texts of the document's style elements. One linear scan over the braces; at-rules
    (`@media ...`) are skipped whole. More than MAX_STYLE_TEXT characters or MAX_RULES rules raises
    TooComplex: dropping rules silently would show text a rule hides."""
    if sum(map(len, blocks)) > MAX_STYLE_TEXT:
        raise TooComplex
    classes: set[str] = set()
    ids: set[str] = set()
    tags: set[str] = set()
    rules = 0
    for block in blocks:
        css = strip_comments(block)
        depth = last = body_start = 0
        prelude = ""
        for m in _BRACES.finditer(css):
            if m[0] == "{":
                if depth == 0:
                    prelude, body_start = css[last:m.start()].strip().lower(), m.end()
                    rules += 1
                    if rules > MAX_RULES:
                        raise TooComplex
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


# --- the tree ------------------------------------------------------------------------------------------


def _tag(node) -> str:  # noqa: ANN001 - a selectolax node
    return (node.tag or "-").lower()


def _breaks_out(svg) -> bool:  # noqa: ANN001
    """Does this svg or math element hold, in Lexbor's tree, an element that ends foreign content in a
    browser? Lexbor keeps a sup inside svg and math where the specification ends them at it, so what follows
    nests differently from a browser's tree and hidden text could surface. Integration points (foreignObject,
    desc, mi ...) legitimately hold HTML; nested svg and math are checked on their own."""
    root_id = svg.mem_id
    points = _INTEGRATION.get(_tag(svg), frozenset())
    node = svg.child
    while node is not None:
        tag = _tag(node)
        descend = True
        if tag[0] not in "-!":
            if tag in _BREAKOUT:
                return True
            if tag == "font" and any(a in node.attributes for a in ("color", "face", "size")):
                return True
            if tag in _FOREIGN or tag in points:
                descend = False
        if descend and node.child is not None:
            node = node.child
            continue
        while node is not None:
            if node.next is not None:
                node = node.next
                break
            node = node.parent
            if node is None or node.mem_id == root_id:
                return False
    return False


def _first_breakout(tree: LexborHTMLParser):  # noqa: ANN202
    for node in tree.css("svg, math"):
        if _breaks_out(node):
            return node
    return None


class _Walk:
    """Text of a parsed document: an iterative depth-first walk (no recursion, so deep trees are fine) that
    skips a hidden element with its whole subtree and emits block and list breaks around the rest."""

    def __init__(self, limit: int, rules: tuple[set[str], set[str], set[str]], styles: bool,
                 stop) -> None:  # noqa: ANN001
        self._limit, self._styles = limit, styles
        self._classes, self._ids, self._tags = rules
        self.parts: list[str] = []
        self.cut = False
        self._size = 0
        self._stop_id = stop.mem_id if stop is not None else None
        self._stop_path: set[int] = set()
        node = stop
        while node is not None:
            self._stop_path.add(node.mem_id)
            node = node.parent

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        self._size += len(text)
        if self._size > self._limit:
            self.cut = True

    def _hides(self, tag: str, attrs: dict[str, str | None], parent_bg: Bg) -> tuple[bool, Bg]:
        if tag in _SKIP:
            return True, parent_bg
        if not self._styles:  # tag-only mode (Gmail)
            return False, None
        a = {k: (v or "") for k, v in attrs.items()}  # the parser keeps the first of a duplicated attribute
        if "hidden" in a or tag in self._tags:
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

    def _open(self, tag: str) -> None:
        if tag == "br" or tag == "hr":
            self._emit("\n")
        elif tag in _BLOCK or tag in _LINES:
            self._emit("\n- " if tag == "li" else "\n")

    def _close(self, tag: str) -> None:
        if (tag in _BLOCK and tag not in ("br", "hr")) or tag == "select":
            self._emit("\n")

    def run(self, root) -> None:  # noqa: ANN001 - a selectolax node
        node = root
        open_tags: list[str] = []
        bgs: list[Bg] = [None]
        while node is not None and not self.cut:
            tag = node.tag or "-"
            parent = open_tags[-1] if open_tags else ""
            if tag == "-text":
                if parent not in ("select", "optgroup"):  # a dropdown shows its options, not stray text
                    self._emit(node.text_content or "")
            elif tag[0] not in "-!":  # an element (not a comment, doctype or document node)
                tag = tag.lower()
                hide, bg = self._hides(tag, node.attributes, bgs[-1])
                if parent in ("select", "optgroup") and tag not in _DROPDOWN:
                    hide = True
                if self._stop_id is not None and node.mem_id in self._stop_path and (
                        hide or node.mem_id == self._stop_id):
                    self.cut = True  # a browser's tree differs from ours from here on: nothing after it
                    return
                if hide:
                    if tag in _BLOCK:
                        self._emit("\n")  # a hidden block still separates what is around it
                else:
                    self._open(tag)
                    if node.child is not None:
                        open_tags.append(tag)
                        bgs.append(bg)
                        node = node.child
                        continue
                    self._close(tag)
            while True:  # next sibling, or back up to the parent's next sibling
                if node.next is not None:
                    node = node.next
                    break
                if not open_tags:
                    node = None
                    break
                self._close(open_tags.pop())
                bgs.pop()
                node = node.parent


def render(markup: str, limit: int, styles: bool) -> tuple[str, bool]:
    """(text, cut): `cut` is True when the text hit `limit` or a browser's tree is known to differ from
    ours from some point on. Raises TooComplex beyond a size or style budget."""
    if len(markup) > MAX_HTML:
        raise TooComplex
    tree = LexborHTMLParser(b"<body>" + markup.encode("utf-8", "replace"))
    rules: tuple[set[str], set[str], set[str]] = (set(), set(), set())
    if styles:
        rules = hiding_rules([node.text() for node in tree.css("style")])
    walk = _Walk(limit, rules, styles, _first_breakout(tree))
    if tree.root is not None:
        walk.run(tree.root)
    return tidy("".join(walk.parts)), walk.cut


# --- the worker ----------------------------------------------------------------------------------------
# Protocol on stdin/stdout, binary: request  = ">BII" (flags: 1 = style rules, limit, byte length) + UTF-8
# markup; reply = ">BI" (0 ok / 1 cut / 2 too complex / 3 failed, byte length) + UTF-8 text. One byte b"R" is
# written once the worker is ready. The worker ends when stdin closes.

CPU_SECONDS = 4  # CPU time one request may use (the parent's wall-clock timeout is the first line)
AS_LIMIT = 256 * 1024 * 1024  # address space, in bytes (an idle worker uses 50 MB, a 1 MB newsletter +40 MB)


def _read_exact(stream, n: int) -> bytes:  # noqa: ANN001
    chunks: list[bytes] = []
    while n > 0:
        chunk = stream.read(n)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def serve() -> None:
    import os
    import resource
    import struct
    import sys

    inp = sys.stdin.buffer
    out_fd = os.dup(1)
    os.dup2(2, 1)  # stray prints can never corrupt the reply stream
    out = os.fdopen(out_fd, "wb", buffering=0)
    os.write(out_fd, b"R")  # imports done, ready for work (before the address-space limit applies)
    resource.setrlimit(resource.RLIMIT_AS, (AS_LIMIT, AS_LIMIT))
    while True:
        try:
            flags, limit, size = struct.unpack(">BII", _read_exact(inp, 9))
            markup = _read_exact(inp, size).decode("utf-8", "replace")
        except EOFError:
            return
        used = resource.getrusage(resource.RUSAGE_SELF)
        cpu = int(used.ru_utime + used.ru_stime) + CPU_SECONDS
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, resource.RLIM_INFINITY))
        try:
            text, cut = render(markup, limit, bool(flags & 1))
            status = 1 if cut else 0
        except TooComplex:
            text, status = NOTICE, 2
        except BaseException:  # noqa: BLE001 - MemoryError, RecursionError, a parser fault: fail closed
            text, status = NOTICE, 3
        data = text.encode("utf-8", "replace")
        out.write(struct.pack(">BI", status, len(data)) + data)


if __name__ == "__main__":
    serve()
