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
# Elements a browser closes on its own when an enclosing block closes: crossing them is not a doubt.
_IMPLIED = frozenset({"p", "li", "dd", "dt", "td", "th", "tr", "tbody", "thead", "tfoot", "option",
                      "optgroup", "colgroup", "caption"})
# Formatting elements a browser re-opens after a block end tag (the adoption agency): only a hidden one of
# these, crossed by a block closer, leaves the extent of the hidden part in doubt.
_FORMATTING = frozenset({"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong",
                         "tt", "u"})
# Elements whose start tag a browser answers by closing an open sibling of these names (top of the stack).
_CLOSES = {"li": frozenset({"li"}), "dd": frozenset({"dd", "dt"}), "dt": frozenset({"dd", "dt"}),
           "td": frozenset({"td", "th"}), "th": frozenset({"td", "th"}),
           "tr": frozenset({"td", "th", "tr"}),
           "tbody": frozenset({"td", "th", "tr", "tbody", "thead", "tfoot"}),
           "thead": frozenset({"td", "th", "tr", "tbody", "thead", "tfoot"}),
           "tfoot": frozenset({"td", "th", "tr", "tbody", "thead", "tfoot"}),
           "option": frozenset({"option"})}
# End tags a browser treats as block closers when deciding what they close (table parts, list items ...).
_CLOSERS = gmail_mime._BLOCK_TAGS | frozenset({
    "td", "th", "dd", "dt", "dl", "tbody", "thead", "tfoot", "caption", "form", "nav", "aside", "main",
    "figure", "fieldset", "address", "center"})
# Elements that stop a browser's search for the element an end tag closes: crossing one means it is ignored.
_SCOPE = frozenset({"table", "td", "th", "caption", "object", "template", "applet", "marquee", "select"})
_SCOPE_EXTRA = {"li": frozenset({"ul", "ol"}), "p": frozenset({"button"})}
_FOREIGN = frozenset({"svg", "math"})  # self-closing children there really are empty elements
_DOUBT = "#doubt"  # stack marker: an end tag crossed a hidden element, so the rest of the parent is hidden
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
        self._close_siblings(tag)
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

    def _close_siblings(self, tag: str) -> None:
        """Pop the open siblings a browser closes by itself (`<li>a<li>b`, `<td>a<td>b`, `<p>a<p>b`), so
        unclosed rows and items do not stack up as depth."""
        closes = _CLOSES.get(tag)
        if closes is None and (tag in _BLOCK or tag == "p"):
            closes = frozenset({"p"})
        while closes and self._stack and self._stack[-1][0] in closes and not self._stack[-1][1]:
            name = self._stack.pop()[0]  # a hidden sibling is left open: when in doubt, keep hiding
            self._open[name] -= 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _FOREIGN or self._open.get("svg") or self._open.get("math"):
            return  # `<path/>` in svg is an empty element: it never stays open
        self.handle_starttag(tag, attrs)  # HTML5 ignores the "/" on a non-void element: `<div/>` opens a div

    def handle_endtag(self, tag: str) -> None:
        if not self._open.get(tag):
            return
        crossed: list[tuple[str, bool, Bg]] = []
        target_hidden = False
        while self._stack:  # every element is pushed and popped once: linear overall
            frame = self._stack.pop()
            name, hid, _ = frame
            if name != _DOUBT:
                self._open[name] -= 1
            if hid:
                self._hidden -= 1
            if name == tag:
                target_hidden = hid
                break
            if name != _DOUBT:
                crossed.append(frame)
        # A browser ignores a non-block end tag that crosses open block elements, and re-opens formatting
        # elements a block end tag crossed. Around a hidden element either means we cannot tell where the
        # hidden part ends, so the rest of the parent is hidden. Other block closers just close what is open.
        if tag in _CLOSERS:  # a block closer only re-opens hidden formatting elements it crossed
            stops = _SCOPE | _SCOPE_EXTRA.get(tag, frozenset())
            doubt = (any(f[1] and (f[0] in _FORMATTING or f[0] in _SKIP) for f in crossed)
                     or ((target_hidden or any(f[1] for f in crossed)) and any(f[0] in stops for f in crossed)))
        else:
            doubt = bool(crossed) and (target_hidden or any(f[1] for f in crossed))
        if doubt:
            self._stack.append((_DOUBT, True, self._stack[-1][2] if self._stack else None))
            self._hidden += 1
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
