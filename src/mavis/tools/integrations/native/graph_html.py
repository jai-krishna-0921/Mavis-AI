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
MAX_RECONSTRUCT_WORK = 100_000  # bound on its re-creation of unclosed formatting elements (see _bounded)
FREE_FORMATTING = 8  # open formatting elements per tag that cost nothing (every real mail has a few)

_SKIP = frozenset({"script", "style", "head", "title", "template", "noscript", "iframe", "object", "svg",
                   "noembed", "noframes", "math", "select"})
_BLOCK = gmail_mime._BLOCK_TAGS
# Elements a browser re-creates (reconstructs) after an end tag or a new block closed them.
_FORMATTING = frozenset({"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong",
                         "tt", "u"})
# Void elements and ones a browser closes by itself: they do not deepen the stack.
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                   "source", "track", "wbr"})
_NOT_NESTING = _VOID | frozenset({"p", "li", "td", "th", "tr", "dd", "dt", "option"})
_CELLS = frozenset({"td", "th"})
_ROWS = frozenset({"tr", "tbody", "thead", "tfoot"})
# Formatting scopes besides cells.
_MARKERS = frozenset({"caption", "object", "applet", "marquee", "template"})
# Raw-text and RCDATA elements: their content is text up to the matching end tag, never markup.
_RAW = frozenset({"script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes"})
_IN_SELECT = frozenset({"option", "optgroup", "hr"})
_FOREIGN = frozenset({"svg", "math"})
_TAG_NAME = re.compile(r"[^\s/>]*")

_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)", re.S)
_BRACES = re.compile(r"[{}]")
_IMPORTANT = re.compile(r"\s*!\s*important\s*$", re.I)
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
    too long to inspect cheaply is treated as hiding. A property that any declaration hides stays hidden
    whatever a later one says (a browser would drop an invalid later value, or honour `!important`, and
    which one applies is not worth guessing): hiding wins."""
    if len(css) > MAX_DECLARATIONS:
        return {"display": "none"}
    css = _CSS_ESCAPE.sub(_unescape, strip_comments(css))
    out: dict[str, str] = {}
    for item in css.split(";"):
        name, sep, value = item.partition(":")
        if sep:
            name = name.strip().lower()
            value = _IMPORTANT.sub("", value.strip()).lower()
            if name in out and name != "color" and style_hides({name: out[name]}, None):
                continue
            out[name] = value
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



_STYLE_OPEN = re.compile(r"<style", re.I)
_STYLE_CLOSE = re.compile(r"</style", re.I)


def _tree_styles(tree: LexborHTMLParser) -> list[str]:
    """The text of each `<style>` element in the tree (comments and inert markup never count)."""
    return [node.text() for node in tree.css("style")]


def _text_styles(markup: str) -> list[str]:
    """The text of each `<style ...>...</style>` found by linear scans, wherever it sits (a comment, an
    attribute, a part of the document that was cut): a superset of the real style elements, so it can only
    hide more."""
    out: list[str] = []
    pos = 0
    while m := _STYLE_OPEN.search(markup, pos):
        start = markup.find(">", m.end())
        if start == -1 or not (end := _STYLE_CLOSE.search(markup, start)):
            break
        out.append(markup[start + 1:end.start()])
        pos = end.end()
    return out


def _capped(blocks: list[str]) -> list[str]:
    """At most MAX_STYLE_TEXT characters of style text in all; later rules are ignored."""
    out: list[str] = []
    total = 0
    for block in blocks:
        if total >= MAX_STYLE_TEXT:
            break
        out.append(block[:MAX_STYLE_TEXT - total])
        total += len(out[-1])
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
    return classes, ids, tags


def _tag_end(markup: str, i: int) -> int:
    """Index of the `>` closing the tag that starts at `i`, honouring quoted attribute values (a quote opens
    a value only right after `=`). A `<` is an ordinary character inside a tag, as in a browser. -1: the
    input ended first."""
    quote = prev = ""
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


def _comment_end(markup: str, i: int) -> int:
    """Index just past the comment that starts at `i` (`<!--`), -1 when it never ends."""
    if markup.startswith(">", i + 4):
        return i + 5
    if markup.startswith("->", i + 4):
        return i + 6
    ends = [(k, n) for k, n in ((markup.find("-->", i + 4), 3), (markup.find("--!>", i + 4), 4)) if k != -1]
    if not ends:
        return -1
    k, n = min(ends)
    return k + n


def _tokens(markup: str) -> list[tuple[int, bool, str]]:
    """(index, is_end_tag, lowercase name) of every tag a browser's tokenizer sees, in one linear pass.
    Comments, bogus comments, quoted attribute values and the text of raw-text elements (script, style,
    textarea, title ...) hold no tags. Once svg or math has started the raw-text rule is off (the tree
    builder decides there), which can only add tags to the list, never remove one."""
    out: list[tuple[int, bool, str]] = []
    pos, foreign = 0, False
    while (i := markup.find("<", pos)) != -1:
        nxt = markup[i + 1:i + 2]
        if markup.startswith("<!--", i):
            pos = _comment_end(markup, i)
            if pos == -1:
                break
            continue
        closing = nxt == "/"
        if nxt in ("!", "?") or (closing and not markup[i + 2:i + 3].isalpha()) or not (
                closing or (nxt.isascii() and nxt.isalpha())):
            if nxt in ("!", "?") or closing:
                j = markup.find(">", i + 1)  # a bogus comment (or `</>`): never tags
                if j == -1:
                    break
                pos = j + 1
            else:
                pos = i + 1  # `1 < 2`: text
            continue
        end = _tag_end(markup, i)
        if end == -1:
            break
        name = _TAG_NAME.match(markup, i + 1 + closing)[0].lower()
        out.append((i, closing, name))
        pos = end + 1
        if closing:
            continue
        if name in _FOREIGN:
            foreign = True
        elif name == "plaintext":
            break
        elif name in _RAW and not foreign and markup[end - 1] != "/":
            m = re.compile(rf"</{name}(?=[\s/>])", re.I).search(markup, pos)
            if m is None:
                break
            pos = m.start()
    return out


def _bounded(tokens: list[tuple[int, bool, str]]) -> int:
    """Index of the first tag past which a browser-style tree builder would cost too much time or memory,
    else -1. Two costs are estimated in one linear pass over the tags, and each only overestimates:
    - nesting: every start tag walks the open elements (to close a p, find a scope ...), so the work is about
      the sum of the open-element depth over the start tags;
    - reconstruction: a formatting element a block or an end tag closed is re-created at the next start tag or
      text, so N unclosed formatting elements (the active list, which table cells and objects scope off) cost
      N clones per following tag. Distinct attributes defeat the browser's three-copies rule, so up to 8 are
      free and the rest are charged on every tag; that bound keeps the memory use small as well."""
    names: list[str] = []
    marks: list[tuple[str, int, int]] = []  # (kind, formatting count, depth) where a scope opened
    fmt = nesting = rebuild = 0

    def leave(kind_stop: str) -> None:
        """Close scopes up to and including the nearest `kind_stop` (the formatting list and the open
        elements fall back to what they were when it opened)."""
        nonlocal fmt
        while marks:
            kind, before, depth = marks.pop()
            if kind != "t":  # formatting a table kept outside its cells stays in the list after it
                fmt = before
            del names[depth:]
            if kind == kind_stop:
                return

    for pos, closing, name in tokens:
        top = marks[-1][0] if marks else ""
        if name in _CELLS:
            if closing:
                if top == "c":
                    leave("c")
            elif top == "c":  # a new cell closes the open one
                leave("c")
                marks.append(("c", fmt, len(names)))
            else:
                marks.append(("c", fmt, len(names)))
        elif name in _ROWS:
            if not closing and top == "c":
                leave("c")
            elif closing and top == "c":
                leave("c")
        elif name == "table":
            if not closing:
                marks.append(("t", fmt, len(names)))
            elif any(k == "t" for k, _, _ in marks):
                leave("t")
        elif name in _MARKERS:
            if not closing:
                marks.append(("o", fmt, len(names)))
            elif any(k == "o" for k, _, _ in marks):
                leave("o")
        rebuild += max(fmt - FREE_FORMATTING, 0)
        if closing:
            if names and names[-1] == name:
                names.pop()
                fmt -= name in _FORMATTING
        else:
            if name not in _VOID:
                nesting += len(names)
            if name in _FORMATTING:
                fmt += 1
                names.append(name)
            elif name not in _NOT_NESTING:
                names.append(name)
        if nesting > MAX_NESTING_WORK or rebuild > MAX_RECONSTRUCT_WORK:
            return pos
    return -1


def _select_doubt(tokens: list[tuple[int, bool, str]]) -> int:
    """Index where the markup must be cut because a `<select>` is not a plain dropdown, else -1. A select
    holds only options, and a browser's rules for anything else in one (a table part, input, keygen, textarea,
    a script, a template, a nested select ...) differ between specification versions and parsers: whether such
    a tag ends the select decides if the text after it is visible. Nothing is guessed: the markup is cut at
    that select. The tokens come from a real tokenizer pass, so a `</select>` in a comment, an attribute or a
    script is not one. A well-formed dropdown (options only, closed) costs nothing."""
    opened = -1
    for pos, closing, name in tokens:
        if opened < 0:
            if name == "select" and not closing:
                opened = pos
        elif name == "select" and closing:
            opened = -1
        elif name not in _IN_SELECT or (closing and name == "hr"):
            return opened
    return -1


def _sup_in_foreign(tokens: list[tuple[int, bool, str]]) -> int:
    """Index of the first svg or math start tag when a sup start tag follows it, else -1. Lexbor keeps a sup
    inside svg and math, where the specification ends the foreign content at it (so what follows is HTML
    again, in the sup). Rewriting sup as another name would let the end tags of the two close each other, so
    nothing is rewritten: svg and math are hidden anyway, and the markup is cut at the first one."""
    first = -1
    for pos, closing, name in tokens:
        if closing:
            continue
        if first < 0 and name in _FOREIGN:
            first = pos
        elif first >= 0 and name == "sup":
            return first
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
    full = markup
    markup = markup[:MAX_HTML]
    tokens = _tokens(markup)
    cuts = [i for i in (_bounded(tokens), _select_doubt(tokens), _sup_in_foreign(tokens)) if i >= 0]
    if cuts:
        markup = markup[:min(cuts)]
    cut = bool(cuts) or len(full) > MAX_HTML
    try:
        tree = LexborHTMLParser(b"<body>" + markup.encode("utf-8", "replace"))
        rules = (set(), set(), set())
        if styles:
            blocks = _tree_styles(tree)
            if cut:  # styles that were cut off (or hide in the dropped part) still apply to what is kept
                blocks += _text_styles(full)
            rules = hiding_rules(_capped(blocks))
        walk = _Walk(limit, rules, styles)
        if tree.root is not None:
            walk.run(tree.root)
    except Exception:  # noqa: BLE001 - a document the parser rejects yields nothing: fail toward hiding
        return "", True
    return gmail_mime.tidy("".join(walk.parts)), cut or walk.cut


def html_to_text(markup: str, limit: int = MAX_HTML) -> str:
    return html_to_text_limited(markup, limit)[0]
