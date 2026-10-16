"""Differential fuzz against a browser-grade parser (html5lib, dev only): any marker word that html5lib
places inside a hidden element (hidden attribute, display:none, or an element we always drop) must not
appear in our text. Visible markers going missing is allowed (fail toward hiding) but bounded."""

import random
import time

import pytest

html5lib = pytest.importorskip("html5lib")

from mavis.tools.integrations.native import gmail_mime, graph_html  # noqa: E402

SKIP = graph_html._SKIP
TAGS = ["div", "span", "p", "b", "i", "a", "font", "table", "tr", "td", "th", "ul", "ol", "li", "button",
        "math", "svg", "iframe", "section", "select", "object", "caption", "template", "noscript", "em",
        "h1", "blockquote", "form", "mtext", "g", "path"]
HIDERS = ["hidden", 'style="display:none"', 'style="visibility:hidden"']


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
        return set(), set()
    hidden: set[str] = set()
    shown: set[str] = set()
    word = re.compile(r"mk\d+z")

    def visit(el, inherited: bool) -> None:
        here = inherited or _is_hidden(el, styles)
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
    for _ in range(n):
        r = rng.random()
        if r < 0.38:
            tag = rng.choice(TAGS)
            attrs = ""
            if rng.random() < 0.3:
                attrs = " " + rng.choice(HIDERS)
            slash = " /" if wild and rng.random() < 0.1 else ""
            parts.append(f"<{tag}{attrs}{slash}>")
            stack.append(tag)
        elif r < 0.62:
            if wild:
                parts.append(f"</{rng.choice(TAGS)}>")
            elif stack:
                parts.append(f"</{stack.pop()}>")
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


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_nothing_a_browser_hides_reaches_the_text(which):
    fn, styles = SANITIZERS[which]
    rng = random.Random(f"oracle-{which}")
    start = time.perf_counter()
    leaks, lost, total_visible = [], 0, 0
    for _ in range(1500):
        html, _n = _doc(rng, rng.randint(4, 40), wild=True)
        hidden, shown = _hidden_markers(html, styles)
        text = fn(html)
        bad = [m for m in hidden if m in text]
        if bad:
            leaks.append((bad, html))
        total_visible += len(shown)
        lost += sum(m not in text for m in shown)
        if time.perf_counter() - start > 20:
            break
    assert not leaks, f"{len(leaks)} leaks, first: {leaks[0]}"
    print(f"{which}: lost {lost} of {total_visible} browser-visible markers in wild documents")
    assert total_visible and lost / total_visible < 0.5, (lost, total_visible)


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
