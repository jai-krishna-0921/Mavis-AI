# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""HTML mail body to text for Outlook and Gmail: a spec-compliant tree (Lexbor) walked iteratively, hidden
content dropped.

Text a reader cannot see but a model can read is the injection channel this guards. The markup is parsed by
a real HTML5 tree builder, so what a browser would nest, close, re-open or ignore is decided by the parser
and not by rules of ours. An element is then dropped (with everything inside it) when its inline style, its
class or id as named by a `<style>` block, or its attributes hide it: display none, visibility hidden,
opacity near 0, tiny font, zero width or height, off-screen offsets, clipping, transparent text, or text
colour that matches (or, with no background set, is) white. Rules inside `@media` blocks are not evaluated
(they would hide the real desktop body). Always dropped by tag: script, style, head, title, template,
noscript, iframe, object, svg, math, noembed, noframes and select (a dropdown is a control, not text).
Comments are never content.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from mavis.tools.integrations.native import gmail_mime

MAX_HTML = gmail_mime.BODY_CAP * 20  # characters of markup parsed
MAX_NESTING_WORK = 40_000_000  # bound on the tree builder's nesting cost (see _bounded)

_SKIP = frozenset({"script", "style", "head", "title", "template", "noscript", "iframe", "object", "svg",
                   "noembed", "noframes", "math", "select"})
_BLOCK = gmail_mime._BLOCK_TAGS
# Elements a browser re-nests when an end tag crosses a block.
_FORMATTING = frozenset({"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong",
                         "tt", "u"})
# Void elements and ones a browser closes by itself: they do not deepen the stack.
_NOT_NESTING = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                          "source", "track", "wbr", "p", "li", "td", "th", "tr", "dd", "dt", "option"})
_TAG_TOKEN = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)")
# Lexbor leaves `<sup>` inside svg and math, where the specification (and so a browser) ends the foreign
# content at it. `<var>` it gets right and parses the same (an ordinary inline element that leaves foreign
# content), and nobody writes it, so sup is written as var.
_SUP = re.compile(r"<(/?)sup(?=[\s/>])", re.I)

_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)", re.S)
_BRACES = re.compile(r"[{}]")
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



def _style_texts(tree: LexborHTMLParser) -> list[str]:
    """The text of each `<style>` element in the tree (comments and inert markup never count), at most
    MAX_STYLE_TEXT characters in all."""
    out: list[str] = []
    total = 0
    for node in tree.css("style"):
        if total >= MAX_STYLE_TEXT:
            break
        text = node.text()[:MAX_STYLE_TEXT - total]
        out.append(text)
        total += len(text)
    return out


def hiding_rules(blocks: list[str]) -> tuple[set[str], set[str], set[str]]:
    """Classes, ids and bare tag names that a `<style>` block hides (colour-only rules do not count).
    `blocks` are the texts of the document's style elements. One linear scan over the braces; at-rules
    (`@media ...`) are skipped whole."""
    classes: set[str] = set()
    ids: set[str] = set()
    tags: set[str] = set()
    for block in blocks:
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
    if "sup" in tags:
        tags.add("var")  # the parse writes sup as var (see _SUP)
    return classes, ids, tags


def _bounded(markup: str) -> tuple[str, bool]:
    """Cut the markup where a browser-style tree builder would start to cost quadratic time. Every start tag
    closes an open p by walking the open elements, and a formatting end tag over a block re-nests them, so the
    work is about the sum of the open-element depth over the tags. It is estimated in one linear scan that
    only ever overestimates the depth (an end tag pops only when it matches the innermost open name); a
    document past the bound is cut there (the rest is dropped and reported)."""
    names: list[str] = []
    work = 0
    for m in _TAG_TOKEN.finditer(markup):
        name = m[2].lower()
        if name in _NOT_NESTING:
            continue
        if m[1]:
            if names and names[-1] == name:
                names.pop()
            if name in _FORMATTING:
                work += len(names)
        else:
            work += len(names)
            names.append(name)
        if work > MAX_NESTING_WORK:
            return markup[:m.start()], True
    return markup, False


_IN_SELECT = frozenset({"option", "optgroup", "hr", "script", "template", "select"})


def _select_doubt(markup: str) -> int:
    """Index where the markup must be cut because a `<select>` is not a plain dropdown, else -1. A select
    holds only options, and a browser's rules for anything else in one (a table part, input, keygen, textarea,
    a nested select, ...) differ between specification versions and parsers: whether such a tag ends the
    select decides if the text after it is visible. Nothing is guessed: the markup is cut at that select, so
    the rest is dropped. A well-formed dropdown (options only, closed) costs nothing."""
    opened = -1
    for m in _TAG_TOKEN.finditer(markup):
        name = m[2].lower()
        if opened < 0:
            if name == "select" and not m[1]:
                opened = m.start()
        elif name == "select":
            if m[1]:
                opened = -1
            else:
                return opened
        elif name not in _IN_SELECT:
            return opened
    return -1


class _Walk:
    """Text of a parsed document: an iterative depth-first walk (no recursion, so deep trees are fine) that
    skips a hidden element with its whole subtree and emits block and list breaks around the rest."""

    def __init__(self, limit: int, rules: tuple[set[str], set[str], set[str]], styles: bool) -> None:
        self._limit, self._styles = limit, styles
        self._classes, self._ids, self._tags = rules
        self.parts: list[str] = []
        self.cut = False
        self._size = 0

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
        elif tag in _BLOCK:
            self._emit("\n- " if tag == "li" else "\n")

    def _close(self, tag: str) -> None:
        if tag in _BLOCK and tag not in ("br", "hr"):
            self._emit("\n")

    def run(self, root) -> None:  # noqa: ANN001 - a selectolax node
        node = root
        open_tags: list[str] = []
        bgs: list[Bg] = [None]
        while node is not None and not self.cut:
            tag = node.tag or "-"
            if tag == "-text":
                self._emit(node.text_content or "")
            elif tag[0] != "-":  # an element (not a comment, doctype or document node)
                tag = tag.lower()
                hide, bg = self._hides(tag, node.attributes, bgs[-1])
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


def html_to_text_limited(markup: str, limit: int, *, styles: bool = True) -> tuple[str, bool]:
    """(text, cut): `cut` is True when the markup, its nesting cost or the text exceeded a bound.
    `styles=False` hides by tag only (no style, class or attribute rules): the Gmail reader."""
    cut = len(markup) > MAX_HTML
    markup, bounded = _bounded(markup[:MAX_HTML])
    doubt = _select_doubt(markup)
    if doubt >= 0:
        markup, bounded = markup[:doubt], True
    cut = cut or bounded
    try:
        tree = LexborHTMLParser(b"<body>" + _SUP.sub(r"<\1var", markup).encode("utf-8", "replace"))
        rules = hiding_rules(_style_texts(tree)) if styles else (set(), set(), set())
        walk = _Walk(limit, rules, styles)
        if tree.root is not None:
            walk.run(tree.root)
    except Exception:  # noqa: BLE001 - a document the parser rejects yields nothing: fail toward hiding
        return "", True
    return gmail_mime.tidy("".join(walk.parts)), cut or walk.cut


def html_to_text(markup: str, limit: int = MAX_HTML) -> str:
    return html_to_text_limited(markup, limit)[0]
