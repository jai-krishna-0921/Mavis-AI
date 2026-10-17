"""Round 5 survivors: sup and var, styles that follow a cut, select text in non-markup, the Gmail cut flag.
The estimator-era tests (reconstruction and depth shapes, the select guard, "hiding wins") went with the
estimator: test_html_isolation.py covers the shapes in one subprocess and the new cascade."""

import base64

import pytest

from mavis.tools.integrations.native import gmail_mime, graph_html

SANITIZERS = {"graph": graph_html.html_to_text, "gmail": gmail_mime.html_to_text}
CAP = graph_html.MAX_HTML


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_a_report_table_with_an_unclosed_font_per_cell_is_not_cut(which):
    row = "<tr><td><font face=Arial>v</td><td><font face=Arial>w</td></tr>"
    html = "<table>" + row * 4500 + "</table><p>END</p>"
    assert len(html) < CAP
    if which == "graph":
        text, cut = graph_html.html_to_text_limited(html, 10**6)
        assert not cut
    else:
        text = gmail_mime.html_to_text(html)
    assert text.count("v") == 4500 and "END" in text


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_unclosed_font_in_implicitly_closed_cells_is_not_cut(which):
    html = "<table>" + "<tr><td><font face=Arial>v" * 9000 + "</table><p>END</p>"
    text = SANITIZERS[which](html)
    assert "END" in text


@pytest.mark.parametrize("html", [
    '<sup style="display:none"></var>INJECT', "<var hidden></sup>INJECT",
    '<sup hidden><b></var>INJECT', "<p>seen</p><var hidden></sup>INJECT",
])
def test_sup_and_var_end_tags_never_close_each_other_outlook(html):
    assert "INJECT" not in graph_html.html_to_text(html)


@pytest.mark.parametrize("which", list(SANITIZERS))
@pytest.mark.parametrize("html", ["<var><svg></sup>INJECT", "<sup><math></var>INJECT",
                                  "<svg><sup>x</sup></svg>INJECT"])
def test_sup_and_var_with_foreign_content_never_leak(which, html):
    text = SANITIZERS[which](html)
    assert "INJECT" not in text


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_sup_and_raw_text_are_untouched(which):
    assert "<sup>2</sup>" in SANITIZERS[which]("<textarea><sup>2</sup></textarea>")
    assert SANITIZERS[which]("x<sup>2</sup> and <var>y</var>") == "x2 and y"


STYLE = "<style>.pre{display:none}</style>"


@pytest.mark.parametrize("html", [
    "<p>Hello</p><span class=pre>INJECT</span><select><option>a<div>b</div></select><p>Bye</p>" + STYLE,
    "<p>Hello</p><span class=pre>INJECT</span><!-- <select><b> --><p>Bye</p>" + STYLE,
    '<p>Hello</p><span class=pre>INJECT</span><p title="<select><b>">Bye</p>' + STYLE,
    "<p>Hello</p><span class=pre>INJECT</span><style>/*" + "<b>" * 9500 + "*/ .pre{display:none}</style>",
    "<p>Hello</p><span class=pre>INJECT</span>" + "<div>" * 9500 + "x" + STYLE,
    "<p>Hello</p><span class=pre>INJECT</span><p>" + "filler " * 60_000 + "</p>" + STYLE,
    "<p>Hello</p><span id=pre>INJECT</span><select><div></select>"
    "<style>/* c */ #pre { display : none }</style>",
])
def test_styles_after_a_cut_still_hide_what_came_before_it(html):
    text = graph_html.html_to_text(html)
    assert "INJECT" not in text and "Hello" in text


@pytest.mark.parametrize("which", list(SANITIZERS))
@pytest.mark.parametrize("html", [
    "<!-- <select><b> --><p>after</p>", '<p title="<select><b>">after</p>',
    "<textarea><select><b></textarea><p>after</p>", "<script>'<select><b>'</script><p>after</p>",
    "<style>/* <select><b> */</style><p>after</p>",
    "<select><option>a<!-- c --><option>b</select><p>after</p>",
])
def test_select_text_in_non_markup_does_not_cut(which, html):
    fn = graph_html.html_to_text_limited if which == "graph" else None
    text, cut = fn("<p>seen</p>" + html, 10**6) if fn else (SANITIZERS[which]("<p>seen</p>" + html), False)
    assert "after" in text and not cut


def test_gmail_reports_a_cut_through_body_text():
    html = "<p>" + "a" * (graph_html.MAX_HTML + 1) + "</p>"
    data = base64.urlsafe_b64encode(html.encode()).decode()
    payload = {"mimeType": "text/html", "body": {"data": data}}
    text, truncated = gmail_mime.body_text(payload)
    assert truncated is True and text == graph_html.NOTICE
    small = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>hi</p>").decode()}}
    assert gmail_mime.body_text(small) == ("hi", False)
