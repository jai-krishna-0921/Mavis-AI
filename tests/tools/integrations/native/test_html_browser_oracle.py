"""Differential fuzz against a browser-grade parser (html5lib, dev only): any marker word that html5lib
places inside a hidden element (hidden attribute, display:none, or an element we always drop) must not
appear in our text. Visible markers going missing is allowed (fail toward hiding) but bounded."""

import random
import re
import time

import pytest

html5lib = pytest.importorskip("html5lib")

from mavis.tools.integrations.native import gmail_mime, graph_html  # noqa: E402

SKIP = graph_html._SKIP
# Every HTML element name a tree builder knows (plus obsolete ones) and the svg and math names that matter.
HTML_TAGS = [
    "a", "abbr", "acronym", "address", "applet", "area", "article", "aside", "audio", "b", "base", "basefont",
    "bdi", "bdo", "bgsound", "big", "blink", "blockquote", "body", "br", "button", "canvas", "caption",
    "center", "cite", "code", "col", "colgroup", "data", "datalist", "dd", "del", "details", "dfn",
    "dir", "div", "dl", "dt", "em", "embed", "fieldset", "figcaption", "figure", "font", "footer", "form",
    "frame", "frameset", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr", "html", "i",
    "iframe", "image", "img", "input", "ins", "kbd", "keygen", "label", "legend", "li", "link", "listing",
    "main", "map", "mark", "marquee", "menu", "menuitem", "meta", "meter", "nav", "nobr", "noembed",
    "noframes", "noscript", "object", "ol", "optgroup", "option", "output", "p", "param", "picture",
    "plaintext", "pre", "progress", "q", "rb", "rp", "rt", "rtc", "ruby", "s", "samp", "script", "search",
    "section", "select", "slot", "small", "source", "span", "strike", "strong", "style", "sub", "summary",
    "sup", "table", "tbody", "td", "textarea", "tfoot", "th", "thead", "time", "title", "tr",
    "track", "tt", "u", "ul", "var", "video", "wbr", "xmp",
]
# Left out because html5lib 1.1 is stale on them and so cannot be the oracle: search (new element, not a
# paragraph closer or block end tag for it), keygen (removed from the specification), summary and menu/dir end
# tags (it ignores `</summary>` where the specification closes the summary), dialog (a paragraph closer),
# rb/rtc (not in its implied-end-tag list), dd/dt/hgroup/figcaption (it foster-parents or misses the block
# the specification has). select: graph_html shows a dropdown's option labels and html5lib 1.1 predates the
# current select parsing (test_html_isolation.py covers select). sup and var are in: an svg or math that
# Lexbor leaves a sup inside is cut from there on, checked on the DOM.
STALE_IN_HTML5LIB = {"select", "search", "keygen", "summary", "dialog", "rb", "rtc", "dd", "dt", "hgroup",
                     "figcaption", "main"}
# No HTML integration points (desc, foreignObject, annotation-xml): html5lib differs from the specification.
FOREIGN_TAGS = ["svg", "math", "g", "path", "circle", "text", "mi", "mo", "mn", "ms", "mtext", "mglyph",
                "malignmark"]
# Weighted toward the names that matter to nesting, so that tables, lists and formatting meet often.
COMMON = ["div", "span", "p", "b", "i", "a", "font", "table", "tr", "td", "th", "ul", "ol", "li", "button",
          "select", "object", "caption", "noscript", "em", "h1", "h2", "h3", "blockquote", "form",
          "svg", "math", "mtext", "g", "path", "iframe", "section", "body", "html", "label", "details",
          "menu", "listing", "frameset", "tbody", "textarea"]
# template is left out: html5lib 1.1 has no template insertion mode (it treats it as a plain element and
# lets a stray end tag leave it open), so it is no oracle for it; test_html_lexbor_regressions.py covers it.
TAGS = [t for t in COMMON * 2 + HTML_TAGS + FOREIGN_TAGS
        if t not in STALE_IN_HTML5LIB | {"template", "li"}]  # li: html5lib foster-parents it into the table
HIDERS = ["hidden", 'style="display:none"', 'style="visibility:hidden"',
          'style="display:none" style="display:block"', 'hidden hidden', 'class="a" hidden class="b"',
          'style="display:block" style="display:none"']
NOT_MARKUP = ["<select><col>", "</select>", "<svg><sup>", "</td></table>", "<style>.a{display:none}</style>",
              "<script>", "<template>", "<table hidden>"]
TABLE_PARTS = {"table", "tr", "td", "th", "col", "colgroup", "tbody", "thead", "tfoot", "caption"}
SKIPPED_ASSERTIONS = [0]  # documents html5lib itself cannot build (an internal assertion)


def _is_hidden(el, styles: bool) -> bool:
    tag = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""  # svg and math carry a namespace
    if tag in SKIP:
        return True
    if not styles:
        return False
    if "hidden" in el.attrib:
        return True
    style = el.attrib.get("style", "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _hidden_markers(html: str, styles: bool) -> tuple[set[str], set[str]]:
    import re

    try:
        root = html5lib.parse("<body>" + html, treebuilder="etree", namespaceHTMLElements=False)
    except AssertionError:  # html5lib 1.1 trips an internal assertion on a few table nestings
        SKIPPED_ASSERTIONS[0] += 1
        return set(), set()
    hidden: set[str] = set()
    shown: set[str] = set()
    word = re.compile(r"mk\d+z")

    def visit(el, inherited: bool) -> None:
        here = inherited or _is_hidden(el, styles)
        if isinstance(el.tag, str):  # a comment's text is no text
            (hidden if here else shown).update(word.findall(el.text or ""))
        for child in el:
            visit(child, here)
            (hidden if here else shown).update(word.findall(child.tail or ""))

    visit(root, False)
    return hidden, shown - hidden


def _doc(rng: random.Random, n: int, wild: bool) -> tuple[str, int]:
    parts: list[str] = []
    stack: list[str] = []
    marks = 0
    foreign = False
    for _ in range(n):
        r = rng.random()
        if r < 0.38:
            tag = rng.choice(TAGS)
            while foreign and tag in TABLE_PARTS:  # html5lib and Lexbor disagree on table parts in svg/math
                tag = rng.choice(TAGS)
            attrs = ""
            if rng.random() < 0.08:  # markup-looking text inside an attribute value is not markup
                attrs = f' title="{rng.choice(NOT_MARKUP)} mk{marks}z"'
                marks += 1
            # html5lib merges a later <body>/<html> start tag's attributes even inside a template, which the
            # specification forbids (and Lexbor follows), so those two never carry attributes here.
            if rng.random() < 0.3 and tag not in ("body", "html"):
                attrs += " " + rng.choice(HIDERS)
            slash = " /" if wild and rng.random() < 0.1 else ""
            parts.append(f"<{tag}{attrs}{slash}>")
            stack.append(tag)
            foreign = foreign or tag in ("svg", "math")
        elif r < 0.62:
            if wild:
                # html5lib's end tags inside svg and math (</p>, </br> that leave them, "any other end tag"
                # passing special elements) differ from the specification, so once one is open only
                # foreign names close
                parts.append(f"</{rng.choice(FOREIGN_TAGS if foreign else TAGS)}>")
            elif stack:
                parts.append(f"</{stack.pop()}>")
        elif r < 0.7:  # a comment holding markup-looking text and a marker that must never appear
            parts.append(f"<!-- {rng.choice(NOT_MARKUP)} mk{marks}z -->")
            marks += 1
        else:
            parts.append(f" mk{marks}z ")
            marks += 1
    if not wild:
        parts.extend(f"</{t}>" for t in reversed(stack))
    return "".join(parts), marks


SANITIZERS = {
    "graph": (graph_html.html_to_text, True),
    "gmail": (gmail_mime.html_to_text, False),
}


DOCS = 6000
TIME_BUDGET = 25.0  # seconds; exceeding it FAILS (a silently shortened run proves less than it claims)
MAX_LOSS = {"graph": 0.025, "gmail": 0.025}  # visible markers allowed to go missing (measured ~0.013)


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_nothing_a_browser_hides_reaches_the_text(which):
    fn, styles = SANITIZERS[which]
    rng = random.Random(f"oracle-{which}")
    start = time.perf_counter()
    SKIPPED_ASSERTIONS[0] = 0
    leaks, lost, total_visible, with_hidden = [], 0, 0, 0
    for _ in range(DOCS):
        html, _n = _doc(rng, rng.randint(4, 40), wild=True)
        skipped = SKIPPED_ASSERTIONS[0]
        hidden, shown = _hidden_markers(html, styles)
        text = fn(html)
        if SKIPPED_ASSERTIONS[0] == skipped:  # markers in comments and attribute values are never text
            inert = set(re.findall(r"mk\d+z", html)) - hidden - shown
            if any(m in text for m in inert):
                leaks.append(([m for m in inert if m in text], html))
        with_hidden += bool(hidden)
        bad = [m for m in hidden if m in text]
        if bad:
            leaks.append((bad, html))
        total_visible += len(shown)
        lost += sum(m not in text for m in shown)
    elapsed = time.perf_counter() - start
    assert not leaks, f"{len(leaks)} leaks, first: {leaks[0]}"
    rate = lost / max(total_visible, 1)
    print(f"{which}: lost {lost} of {total_visible} browser-visible markers ({rate:.4f}), "
          f"{with_hidden}/{DOCS} documents with a hidden marker, {SKIPPED_ASSERTIONS[0]} html5lib skips, "
          f"{elapsed:.1f}s")
    assert elapsed < TIME_BUDGET, f"oracle run took {elapsed:.1f}s"
    assert SKIPPED_ASSERTIONS[0] < DOCS * 0.01, SKIPPED_ASSERTIONS[0]
    assert with_hidden > DOCS * 0.3, with_hidden
    assert total_visible and lost / total_visible < MAX_LOSS[which], (lost, total_visible)


_CHILD = {"ul": "li", "ol": "li", "table": "tr", "tr": "td"}
_INLINE = ["span", "b", "i", "a", "em", "font"]
_BLOCKS = ["div", "p", "blockquote", "section", "ul", "ol", "table", "h1"]
_WRAP = ["noscript", "iframe", "svg", "math", "object"]


def _hider(rng: random.Random, p: float) -> str:
    return (" " + rng.choice(HIDERS)) if rng.random() < p else ""


def _tame(rng: random.Random, depth: int, counter: list[int], inline: bool = False,
          in_a: bool = False) -> str:
    """A valid fragment (inline elements hold only inline content, p holds only inline content, no a in a),
    with hidden and visible elements mixed and a marker word in each text."""
    out = []
    for _ in range(rng.randint(1, 3)):
        r = rng.random()
        if r < 0.4 or depth >= 4:
            out.append(f" mk{counter[0]}z ")
            counter[0] += 1
            continue
        if r > 0.94:
            out.append(_wrapped(rng, counter))
            continue
        tag = rng.choice(_INLINE if inline or r < 0.6 else _BLOCKS)
        if tag == "a" and in_a:
            tag = "span"
        attrs = _hider(rng, 0.25)
        if tag in _CHILD:
            inner = _rows(rng, tag, depth, counter)
        else:
            inner = _tame(rng, depth + 1, counter, inline or tag in _INLINE or tag in ("p", "h1"),
                          in_a or tag == "a")
        out.append(f"<{tag}{attrs}>{inner}</{tag}>")
    return "".join(out)


def _wrapped(rng: random.Random, counter: list[int]) -> str:
    tag = rng.choice(_WRAP)
    text = f" mk{counter[0]}z "  # always-dropped elements hold plain text
    counter[0] += 1
    return f"<{tag}{_hider(rng, 0.3)}>{text}</{tag}>"


def _rows(rng: random.Random, tag: str, depth: int, counter: list[int]) -> str:
    child = _CHILD[tag]
    rows = []
    for _ in range(rng.randint(1, 3)):
        inner = _rows(rng, child, depth + 1, counter) if child in _CHILD else _tame(rng, depth + 1, counter)
        rows.append(f"<{child}{_hider(rng, 0.2)}>{inner}</{child}>")
    return "".join(rows)


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_well_formed_documents_leak_nothing_and_keep_every_visible_marker(which):
    fn, styles = SANITIZERS[which]
    rng = random.Random(f"wellformed-{which}")
    lost, leaks, seen = [], [], 0
    for _ in range(800):
        html = _tame(rng, 0, [0])
        hidden, shown = _hidden_markers(html, styles)
        text = fn(html)
        seen += len(shown)
        leaks += [m for m in hidden if m in text]
        lost += [(m, html) for m in shown if m not in text]
    assert not leaks
    assert seen > 500
    assert not lost, (len(lost), lost[0])


# --- round 6: sup inside svg/math (Lexbor keeps it there, browsers do not) and dropdowns -----------------
SUP_TAGS = ["svg", "math", "sup", "sup", "sup", "span", "div", "p", "b", "i", "g", "mi", "mtext", "var",
            "table", "td", "font", "a"]
SUP_HIDERS = ["hidden", 'style="display:none"']


def _sup_doc(rng: random.Random) -> str:
    parts, marks = [], 0
    for _ in range(rng.randint(3, 14)):
        r = rng.random()
        if r < 0.45:
            tag = rng.choice(SUP_TAGS)
            attr = (" " + rng.choice(SUP_HIDERS)) if rng.random() < 0.35 else ""
            parts.append(f"<{tag}{attr}>")
        elif r < 0.7:
            parts.append(f"</{rng.choice([t for t in SUP_TAGS if t != 'p'])}>")
        else:
            parts.append(f" mk{marks}z ")
            marks += 1
    return "".join(parts)


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_sup_in_svg_and_math_leaks_nothing(which):
    """The 94 distinct leak shapes of re-review 5 (2c) came from this generator with the guard disabled."""
    fn, styles = SANITIZERS[which]
    rng = random.Random(f"sup-{which}")
    leaks, with_sup = [], 0
    for _ in range(4000):
        html = _sup_doc(rng)
        hidden, _shown = _hidden_markers(html, styles)
        with_sup += "<sup" in html and ("<svg" in html or "<math" in html)
        text = fn(html)
        bad = [m for m in hidden if m in text]
        if bad:
            leaks.append((bad, html))
    assert with_sup > 500
    assert not leaks, f"{len(leaks)} leaks, first: {leaks[0]}"


def _select_doc(rng: random.Random, marks: list[int]) -> tuple[str, set[str], set[str]]:
    """A well-formed dropdown: option labels are visible, stray text and hidden options are not."""
    def mk() -> str:
        marks[0] += 1
        return f"mk{marks[0]}z"

    shown, hidden, body = set(), set(), []
    for _ in range(rng.randint(1, 5)):
        r = rng.random()
        word = mk()
        if r < 0.15:
            body.append(f" {word} ")
            hidden.add(word)
        elif r < 0.3:
            body.append(f"<option hidden>{word}</option>")
            hidden.add(word)
        elif r < 0.45:
            inner = mk()
            body.append(f"<optgroup label=g><option>{inner}</option></optgroup>")
            shown.add(inner)
        else:
            body.append(f"<option>{word}</option>")
            shown.add(word)
    return f"<p>{mk()}</p><select>{''.join(body)}</select>", shown, hidden


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_a_dropdown_shows_its_options_and_nothing_else(which):
    fn, _styles = SANITIZERS[which]
    rng = random.Random(f"select-{which}")
    marks = [0]
    for _ in range(400):
        html, shown, hidden = _select_doc(rng, marks)
        text = fn(html)
        if which == "graph":  # Gmail never reads the hidden attribute, so a hidden option stays there
            assert not [m for m in hidden if m in text], html
            assert all(m in text for m in shown), html
        else:
            assert not [m for m in hidden if m in text and f">{m}</option>" not in html], html
