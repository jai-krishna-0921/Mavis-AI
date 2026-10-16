"""Leak shapes from final-rereview-2 and -3, duplicate attributes and heading end tags, select and template,
and linear time on pathological input. A browser (html5lib) agrees with every expectation here."""

import time

import pytest

from mavis.tools.integrations.native import gmail_mime, graph_html

SANITIZERS = {"graph": graph_html.html_to_text, "gmail": gmail_mime.html_to_text}

# Rereview-3 NB1/NB2 and rereview-2 NB1/NB2: Outlook path (style and hidden attributes decide).
GRAPH_SHAPES = [
    "<body><span style=\"display:none\"></body>SECRET",
    "<body><span hidden></html>SECRET",
    "<body><label style=\"display:none\"></body>SECRET",
    "<body><ruby hidden></body>SECRET",
    "<body><span hidden><details></span>SECRET",
    "<body><span hidden><summary></span>SECRET",
    "<body><label style=\"display:none\"><menu></label>SECRET",
    "<body><nobr><listing style=\"display:none\"></nobr>SECRET",
    "<mi style=\"display:none\"><menu></mi>SECRET",
    "<body><keygen><span hidden></keygen>SECRET",
    "<body><image><span hidden></image>SECRET",
    "<p style=\"display:none\" style=\"display:block\">SECRET</p>",
    "<p hidden hidden=\"\" class=a class=b>SECRET</p>",
    "<body><h2></h1><span style=\"display:none\"></h2>SECRET",
    "<div style=\"display:none\"><table></div>SECRET</table>",
    "<div style=\"display:none\"><table><tr><td>x</div>SECRET",
    "<div><table><tr><td><span style=\"display:none\">x</div>SECRET",
    "<p hidden><table><tr><td>x</p>SECRET",
    "<li style=\"display:none\"><ul></li>SECRET",
    "<p hidden><button>x</p>SECRET",
    "<math><span style=\"display:none\"/>SECRET</math> tail",
    "<math><p hidden/>SECRET",
    "<math><mtext><span style=\"display:none\"/>SECRET",
    "<div hidden><select></div>SECRET",
    "<span hidden><select><option>a</select>SECRET",
    "<div hidden><template>x</div>SECRET",
]
# Hidden by tag on both paths.
BOTH_SHAPES = [
    "<body><svg></body>SECRET",
    "<body><svg></html>SECRET",
    "<body><svg><g></body>SECRET",
    "<body><svg><desc></body>SECRET",
    "<body><math><mi></body>SECRET",
    "<div><iframe>x</div>SECRET</iframe>",
    "<p><iframe>x</p>SECRET</iframe>",
    "<div><object>x</div>SECRET</object>",
    "<noscript><details></noscript>SECRET",
    "<select><option>a</option></select><template>SECRET</template>",
    "<template>SECRET",
    "<select><option>SECRET",
]


@pytest.mark.parametrize("html", GRAPH_SHAPES)
def test_graph_shape_leaks_nothing(html):
    assert "SECRET" not in graph_html.html_to_text(html)


@pytest.mark.parametrize("which", list(SANITIZERS))
@pytest.mark.parametrize("html", BOTH_SHAPES)
def test_both_paths_leak_nothing(which, html):
    assert "SECRET" not in SANITIZERS[which](html)


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_survey_mail_keeps_what_follows_a_dropdown(which):
    html = ("<table><tr><td><form><select><option>Great</option><option>Poor</option></select>"
            "<input type=submit></form><p>Thanks for your answer</p></td></tr></table><p>Footer text</p>")
    text = SANITIZERS[which](html)
    assert "Thanks for your answer" in text and "Footer text" in text
    assert "Great" not in text and "Poor" not in text


def test_text_after_select_stays_visible_between_paragraphs():
    text = graph_html.html_to_text("<p>VIS1</p><select><option>x</option></select><p>rest of the mail</p>")
    assert "VIS1" in text and "rest of the mail" in text


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_many_unclosed_hidden_items_do_not_swallow_the_visible_text_after_them(which):
    html = "<ul>" + '<li style="display:none">x' * 120 + "</ul><p>VISIBLE_AFTER</p>"
    fn = graph_html.html_to_text_limited if which == "graph" else None
    text, cut = (fn(html, 20_000) if fn else (gmail_mime.html_to_text(html), False))
    assert "VISIBLE_AFTER" in text and not cut or which == "gmail"
    html = "<table>" + '<tr><td hidden>x' * 120 + "</table><p>VISIBLE_AFTER</p>"
    assert "VISIBLE_AFTER" in SANITIZERS[which](html)


def test_duplicate_attribute_keeps_the_first_like_a_browser():
    assert graph_html.html_to_text('<p style="display:none" style="display:block">SECRET</p>') == ""
    assert graph_html.html_to_text('<p style="display:block" style="display:none">SHOWN</p>') == "SHOWN"


def test_explicit_body_end_tag_does_not_close_a_hidden_inline_element():
    assert graph_html.html_to_text('<body><p>seen</p><span style="display:none"></body>SECRET') == "seen"


PATHOLOGICAL = {
    "100k nested b": "<b>" * 100_000 + "x",
    "100k nested hidden spans": '<span style="display:none">' * 100_000 + "x",
    "20k unclosed table rows": "<table>" + "<tr><td>a" * 20_000 + "</table>END",
    "20k unclosed hidden rows": "<table>" + '<tr style="display:none"><td>a' * 20_000 + "</table>END",
    "deep divs": "<div>" * 60_000 + "x" + "</div>" * 60_000,
    "unclosed attrs": "<a " * 120_000,
    "comments": "<!--" * 90_000,
    "lone brackets": "<" * 390_000,
    "formatting churn": "<b><i><div hidden>" * 20_000 + "</b></i>" * 20_000,
}


@pytest.mark.parametrize("name", list(PATHOLOGICAL))
@pytest.mark.parametrize("which", list(SANITIZERS))
def test_pathological_input_is_linear(which, name):
    start = time.perf_counter()
    SANITIZERS[which](PATHOLOGICAL[name])
    assert time.perf_counter() - start < 1.5, (which, name)


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_hidden_list_items_then_visible_text(which):
    html = "<ul>" + "<li hidden>x" * 120 + "</ul>VISIBLE"
    assert "VISIBLE" in SANITIZERS[which](html)


@pytest.mark.parametrize("which", list(SANITIZERS))
@pytest.mark.parametrize("html", [
    "<table><select><col>SECRET</select>",
    "<table><select><colgroup>SECRET",
    "<select><textarea></textarea><select>SECRET",
    "<select><option>a<select>SECRET",
    "<select><option>a<input>SECRET",
    "<select><option title=\"</select>\"><col>SECRET",
])
def test_select_and_foreign_ambiguities_fail_toward_hiding(which, html):
    text = SANITIZERS[which]("<p>seen</p>" + html)
    assert "SECRET" not in text and "seen" in text


def test_sup_leaves_svg_like_a_browser_so_its_style_still_applies():
    # Lexbor keeps sup inside svg; graph_html parses it as var, which leaves svg as the specification says
    assert "SECRET" not in graph_html.html_to_text('<p>seen</p><svg><sup style="display:none"><h1>SECRET')
    assert "SECRET" not in graph_html.html_to_text('<p>seen</p><math><sup hidden><div>SECRET')


@pytest.mark.parametrize("html", [
    "<p>a</p><body hidden>SECRET", '<p>a</p><html style="display:none">SECRET',
    '<body hidden><p>SECRET</p></body>',
])
def test_a_hiding_body_or_html_hides_the_message_like_a_browser(html):
    assert "SECRET" not in graph_html.html_to_text(html)


def test_sup_still_reads_normally():
    assert graph_html.html_to_text("x<sup>2</sup> y") == "x2 y"
    assert gmail_mime.html_to_text("x<SUP>2</SUP> y") == "x2 y"
    assert "SECRET" not in graph_html.html_to_text("<style>sup{display:none}</style>a<sup>SECRET</sup>")


def test_a_hidden_block_still_breaks_the_line():
    assert graph_html.html_to_text("a<p hidden>x</p>b") == "a\nb"


def test_comments_and_processing_instructions_are_never_text():
    text = graph_html.html_to_text("a<!-- SECRET -->b<?pi SECRET?>c<![CDATA[SECRET]]>d")
    assert "SECRET" not in text
