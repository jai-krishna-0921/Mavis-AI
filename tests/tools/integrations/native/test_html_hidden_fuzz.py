"""Hidden-HTML regressions shared by the Outlook and Gmail sanitizers: mis-nested hidden blocks, noembed and
noframes, end-tag attribute text, and linear time on pathological nesting. Rule: when in doubt, hide."""

import random
import time

import pytest

from mavis.tools.integrations.native import gmail_mime, graph_html

SANITIZERS = {"graph": graph_html.html_to_text, "gmail": gmail_mime.html_to_text}

# Hidden by every sanitizer (Gmail hides script/style/noscript/noembed/noframes/... by tag).
SECRET_CASES = {
    "noembed": "<p>ok</p><noembed>SECRET</noembed>",
    "noframes": "<p>ok</p><noframes>SECRET</noframes>",
    "noembed with markup": "<noembed><b>SECRET</b></noembed>",
    "end tag attribute text": '<p>ok</p></p class="x>SECRET">',
    "end tag attribute text single quote": "<p>ok</p></p title='>SECRET'>",
}
# Needs a hiding rule that only the Outlook sanitizer has (style/hidden attributes).
GRAPH_ONLY = {
    "cross end tag pops hidden block": "<span><div hidden>A</span>SECRET</div>",
    "cross end tag pops hidden section": "<span hidden><section></span>SECRET",
    "cross end tag pops styled ul": "<span style=display:none><ul></span>SECRET",
    "formatting reconstruction": "<div><b hidden>A</div>SECRET",
    "formatting reconstruction styled": '<div><font style="display:none">A</div>SECRET',
    "hidden target with open block": "<span hidden><p>A</span>SECRET",
}
# Gmail hides these by tag, so a mis-nested close must not reveal what follows.
GMAIL_NESTED = {
    "noscript crossed": "<b><noscript>A</b>SECRET",
    "noembed crossed": "<b><noembed>A</b>SECRET</noembed>",
    "svg crossed": "<i><svg>A</i>SECRET",
}


@pytest.mark.parametrize("name", list(SECRET_CASES))
@pytest.mark.parametrize("which", list(SANITIZERS))
def test_secret_never_reaches_text(which, name):
    text = SANITIZERS[which](SECRET_CASES[name])
    assert "SECRET" not in text and ">" not in text.replace("&gt;", ""), text


@pytest.mark.parametrize("name", list(GRAPH_ONLY))
def test_graph_mis_nested_hidden_blocks_stay_hidden(name):
    assert "SECRET" not in graph_html.html_to_text(GRAPH_ONLY[name])


@pytest.mark.parametrize("name", list(GMAIL_NESTED))
def test_gmail_mis_nested_hidden_tags_stay_hidden(name):
    assert "SECRET" not in gmail_mime.html_to_text(GMAIL_NESTED[name])


def test_closing_a_block_normally_still_shows_what_follows():
    assert graph_html.html_to_text("<div hidden>A</div>after") == "after"
    assert graph_html.html_to_text("<div hidden><p>A</div>after") == "after"  # </div> implicitly closes the p
    assert graph_html.html_to_text("<div><p hidden>A</div>after") == "after"
    assert gmail_mime.html_to_text("<noscript>A</noscript>after") == "after"
    assert gmail_mime.html_to_text("<p>one</p><p>two</p>") == "one\n\ntwo"


# --- seeded fuzz: a hidden element that is never closed hides everything after it ---
_JUNK = ["span", "b", "i", "section", "ul", "li", "a", "em", "table", "td", "tr"]


def _junk(rng: random.Random, skip: str) -> str:
    out = []
    for _ in range(rng.randint(3, 25)):
        tag = rng.choice([t for t in _JUNK if t != skip])
        kind = rng.random()
        if kind < 0.45:
            out.append(f"<{tag}>")
        elif kind < 0.85:
            out.append(f"</{tag}>")
        elif kind < 0.92:
            out.append(f'</{tag} x="a>SECRET">')
        else:
            out.append("text ")
    return "".join(out)


@pytest.mark.parametrize("opener", ["div hidden", "span hidden", "b hidden", "section hidden",
                                    "ul hidden", 'div style="display:none"', 'span style="font-size:0"',
                                    'b style="color:#fff"'])
def test_unclosed_hidden_element_hides_everything_after_graph(opener):
    skip = opener.split()[0]
    rng = random.Random(f"graph-{opener}")
    for _ in range(150):
        html = f"<p>seen</p><{opener}>" + _junk(rng, skip) + "SECRET" + _junk(rng, skip) + "SECRET"
        text = graph_html.html_to_text(html)
        assert "SECRET" not in text, html
        assert "seen" in text


@pytest.mark.parametrize("opener", ["noscript", "template", "noembed", "noframes", "title", "script"])
def test_unclosed_skipped_element_hides_everything_after_gmail(opener):
    rng = random.Random(f"gmail-{opener}")
    for _ in range(150):
        html = f"<p>seen</p><{opener}>" + _junk(rng, opener) + "SECRET" + _junk(rng, opener) + "SECRET"
        text = gmail_mime.html_to_text(html)
        assert "SECRET" not in text, html
        assert "seen" in text


def test_hidden_formatting_element_crossed_by_an_end_tag_is_reopened_so_the_rest_stays_hidden():
    # a browser re-opens the hidden <b> for every later text, so even what follows </div> is hidden
    text = graph_html.html_to_text("<div><span><b hidden>A</span>SECRET</div>later")
    assert "SECRET" not in text and "later" not in text


# --- linear time and size bounds, both modules ---
PATHOLOGICAL = {
    "unclosed tags": "<a " * 120_000,
    "unclosed html comments": "<!--" * 90_000,
    "unclosed end tags": "</a " * 97_000,
    "lone angle brackets": "<" * 390_000,
    "unclosed quotes": '<a b="' * 65_000,
    "deep nesting": "<div>" * 60_000 + "x" + "</div>" * 60_000,
    "deep hidden nesting": '<span style="display:none">' * 14_000,
    "crossing end tags": "<b><i><div hidden>" * 20_000 + "</b></i>" * 20_000,
    "unclosed noembed": "<noembed>" * 50_000,
    "declarations": "<!x " * 97_000 + "<![if x]>" * 40_000,
    "end tag attributes": '</p x=">">' * 90_000,
}


@pytest.mark.parametrize("name", list(PATHOLOGICAL))
@pytest.mark.parametrize("which", list(SANITIZERS))
def test_pathological_markup_is_linear(which, name):
    start = time.perf_counter()
    SANITIZERS[which](PATHOLOGICAL[name])
    assert time.perf_counter() - start < 1.5, (which, name)


def test_gmail_html_input_is_size_capped():
    huge = "<p>seen</p>" + "a" * (graph_html.MAX_HTML * 2) + "<p>LATE</p>"
    text = gmail_mime.html_to_text(huge)
    assert "seen" in text and "LATE" not in text


# --- fix round 2: no over-hiding on block closers, svg children, unclosed siblings ---
@pytest.mark.parametrize("html,shown", [
    ('<div style="display:none">Preview<span></div><table><tr><td>Real content</td></tr></table>',
     "Real content"),
    ("<table><tr><td><span style=display:none>x</td><td>Cell two</td></tr></table>", "Cell two"),
    ("<div><p hidden>x</div>Visible", "Visible"),
])
def test_block_closer_over_a_hidden_inline_child_keeps_the_rest_visible(html, shown):
    assert shown in graph_html.html_to_text(html)


def test_block_closer_over_a_hidden_formatting_element_still_hides():
    assert "SECRET" not in graph_html.html_to_text("<div><b hidden>A</div>SECRET")
    assert "SECRET" not in graph_html.html_to_text('<div><a style="display:none">A</div>SECRET')


SVG = '<a href="https://x.test">Go <svg width="9"><path d="M0"/><circle r="1"/></svg>Label</a> after'


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_svg_self_closing_children_do_not_hide_nearby_text(which):
    text = SANITIZERS[which](SVG)
    assert "Label" in text and "after" in text


def _rows(n):
    return "<table>" + "".join(f"<tr><td>name{i}<td>val{i}" for i in range(n)) + "</table><p>END</p>"


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_unclosed_rows_items_and_paragraphs_do_not_count_toward_depth(which):
    text = SANITIZERS[which](_rows(40))
    assert "val39" in text and "END" in text
    text = SANITIZERS[which](_rows(3000))
    assert "val2999" in text and "END" in text
    text = SANITIZERS[which]("<ul>" + "".join(f"<li>item{i}" for i in range(500)) + "</ul>END")
    assert "item499" in text and "END" in text
    text = SANITIZERS[which]("".join(f"<p>para{i}" for i in range(500)) + "END")
    assert "para499" in text and "END" in text


def test_implied_closes_do_not_leak_hidden_text():
    assert "SECRET" not in graph_html.html_to_text("<ul><li hidden>a<li>b</ul><p hidden>SECRET")


# --- fix round 3: ignored block closers, iframe raw text, math breakout ---
IGNORED_CLOSER = [
    '<div style="display:none"><table></div>SECRET</table>',
    '<div style="display:none"><table><tr><td>x</div>SECRET',
    '<div><table><tr><td><span style="display:none">x</div>SECRET',
    "<p hidden><table><tr><td>x</p>SECRET",
    '<li style="display:none"><ul></li>SECRET',
    "<p hidden><button>x</p>SECRET",
    "<div hidden><select></div>SECRET",
    "<div hidden><marquee></div>SECRET",
    "<div hidden><object></div>SECRET",
]


@pytest.mark.parametrize("html", IGNORED_CLOSER)
def test_block_closer_a_browser_ignores_never_closes_a_hidden_element(html):
    assert "SECRET" not in graph_html.html_to_text(html)


@pytest.mark.parametrize("which", list(SANITIZERS))
@pytest.mark.parametrize("html", [
    "<div><iframe>x</div>SECRET</iframe>", "<p><iframe>x</p>SECRET</iframe>",
    "<div><object>x</div>SECRET</object>", "<div><iframe>x</div>SECRET",
])
def test_iframe_and_object_content_is_never_text(which, html):
    assert "SECRET" not in SANITIZERS[which](html)


@pytest.mark.parametrize("html", [
    '<math><span style="display:none"/>SECRET</math> tail', "<math><p hidden/>SECRET",
    '<math><mtext><span style="display:none"/>SECRET',
])
def test_math_breakout_tags_stay_hidden(html):
    assert "SECRET" not in graph_html.html_to_text(html)


def test_math_breakout_keeps_what_follows_hidden_like_a_browser():
    # html5lib: the <span> leaves math and stays open, so " tail" is inside it; hiding it fails safe
    assert graph_html.html_to_text('<math><span style="display:none"/>SECRET</math> tail') == ""
    assert graph_html.html_to_text("<svg><path d='M0'/><g/></svg>tail") == "tail"


async def _post_fail(first, second, **kw):
    import httpx as _h

    from mavis.domain.errors import FailureKind as _K  # noqa: F401
    from mavis.tools.integrations.native.graph import GraphError

    from .graphfake import FakeGraph, graph_error

    answers = [first, second]

    def handler(request):
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(a, Exception):
            raise a
        return a

    g = FakeGraph().on("POST", r"/me/sendMail$", handler)
    ex, _ = g.executor()
    with pytest.raises(GraphError) as e:
        await ex.graph.json(1, "POST", "https://graph.microsoft.com/v1.0/me/sendMail", body={}, **kw)
    return e.value, graph_error, _h


async def test_never_sent_writes_keep_unavailable_or_rate_limited():
    import httpx as h

    from mavis.domain.errors import FailureKind as K

    req = h.Request("POST", "https://graph.microsoft.com/v1.0/me/sendMail")
    err, graph_error, _ = await _post_fail(h.ConnectError("x", request=req), h.ConnectError("x", request=req))
    assert err.kind is K.UNAVAILABLE
    slow = graph_error(429, "x", headers={"Retry-After": "99"})
    err, *_ = await _post_fail(h.ConnectError("x", request=req), slow)
    assert err.kind is K.RATE_LIMITED
    err, *_ = await _post_fail(h.PoolTimeout("x", request=req), h.ConnectTimeout("x", request=req))
    assert err.kind is K.UNAVAILABLE


# --- shapes found by the html5lib differential fuzz (test_html_browser_oracle.py) ---
ORACLE_SHAPES = [
    "<i><path><noscript></path></i>SECRET",
    '<li style="visibility:hidden"><a><ul></a></li>SECRET',
    '<form><th style="display:none"><a hidden></th></form>SECRET',
    "<caption><ol hidden></caption>SECRET",
    '<td><path style="visibility:hidden"></td>SECRET',
    '<form><blockquote style="visibility:hidden"></form>SECRET',
    '<th><li style="visibility:hidden"></th>SECRET',
    '<th><ul style="visibility:hidden"></th>SECRET',
    "<b><i hidden></b>SECRET",
    "<span><b hidden></span>SECRET",
    "<div><b hidden><p></b>SECRET",
    '<p hidden><div style="visibility:hidden"></p>SECRET',
    '<table><td></tr><blockquote style="visibility:hidden"></td>SECRET',
    '<li><div><li style="display:none"></div>SECRET',
    "<svg><p hidden /></svg>SECRET",
    '<table><ol><tr><section style="display:none"></ol>SECRET',
    "<form hidden><a></form>SECRET",
    '<select><select><object style="display:none"></select>SECRET',
    '<button><li style="visibility:hidden"></button><noscript></li>SECRET',
    '<math><mtext><div style="display:none"/>SECRET',
]


@pytest.mark.parametrize("html", ORACLE_SHAPES)
def test_oracle_shapes_never_leak_on_the_outlook_path(html):
    assert "SECRET" not in graph_html.html_to_text(html)
