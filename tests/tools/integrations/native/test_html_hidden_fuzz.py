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


def test_hidden_block_closed_by_a_crossing_tag_hides_the_rest_of_the_parent_only():
    text = graph_html.html_to_text("<div><span><b hidden>A</span>SECRET</div>visible")
    assert "SECRET" not in text and "visible" in text


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
