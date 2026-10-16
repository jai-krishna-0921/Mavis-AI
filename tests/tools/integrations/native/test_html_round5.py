"""Round 5: the nesting/reconstruction cost bound, sup and var, styles after a cut, the select guard, the
inline-style cascade and the Gmail cut flag. Each expectation agrees with WHATWG parsing (html5lib where it
is current)."""

import json
import subprocess
import sys
import textwrap

import pytest

from mavis.tools.integrations.native import gmail_mime, graph_html

SANITIZERS = {"graph": graph_html.html_to_text, "gmail": gmail_mime.html_to_text}
CAP = 400_000

_RUNNER = textwrap.dedent("""
    import json, resource, sys, time
    resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
    from mavis.tools.integrations.native import gmail_mime, graph_html
    html = sys.stdin.read()
    out = {}
    for name, fn in (("graph", graph_html.html_to_text_limited), ("gmail", None)):
        start = time.perf_counter()
        try:
            r = fn(html, 10**6) if fn else (gmail_mime.html_to_text(html), False)
            out[name] = [time.perf_counter() - start, r[1], len(r[0])]
        except MemoryError:
            out[name] = [time.perf_counter() - start, "memory", 0]
    out["rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(json.dumps(out))
""")


def _measure(html: str) -> dict:
    p = subprocess.run([sys.executable, "-c", _RUNNER], input=html[:CAP], capture_output=True, text=True,
                       timeout=120, check=True)
    return json.loads(p.stdout)


def _fmt_prefix(n: int, wrap: str = "div") -> str:
    return f"<{wrap}>" + "".join(f"<b id={i}>" for i in range(n)) + f"</{wrap}>"


RECONSTRUCT = {
    "li": _fmt_prefix(3000) + "<ul>" + "<li>x" * 100_000,
    "li small": _fmt_prefix(1500) + "<ul>" + "<li>x" * 100_000,
    "p": _fmt_prefix(3000) + "<p>x" * 100_000,
    "dd": _fmt_prefix(3000) + "<dl>" + "<dd>x" * 100_000,
    "dt": _fmt_prefix(3000) + "<dl>" + "<dt>x" * 100_000,
    "td": _fmt_prefix(3000) + "<table><tr>" + "<td>x" * 100_000,
    "option": _fmt_prefix(3000) + "<option>x" * 100_000,
    "div rounds": _fmt_prefix(3000) + "<div>x</div>" * 40_000,
    "text rounds": _fmt_prefix(3000) + "x<p>" * 100_000,
    "nested tables": _fmt_prefix(3000) + "<table><tr><td>x</table>" * 20_000,
    "in a cell": "<table><tr><td>" + "".join(f"<b id={i}>" for i in range(3000)) + "<li>x" * 100_000,
}


@pytest.mark.parametrize("name", list(RECONSTRUCT))
def test_formatting_reconstruction_shapes_stay_cheap_in_time_and_memory(name):
    m = _measure(RECONSTRUCT[name])
    assert m["graph"][0] < 1.0 and m["gmail"][0] < 1.0, m
    assert "memory" not in (m["graph"][1], m["gmail"][1]), m
    assert m["rss_mb"] < 500, m


DEPTH = {
    "p pairs over deep divs": "<div>" * 8000 + "<p></p>" * 80_000,
    "li pairs over deep divs": "<div>" * 8000 + "<li></li>" * 60_000,
    "td over deep spans": "<table><tr>" + "<span>" * 8000 + "<td>x" * 80_000,
}


@pytest.mark.parametrize("name", list(DEPTH))
def test_block_starts_that_do_not_deepen_the_stack_are_charged_for_the_walk(name):
    m = _measure(DEPTH[name])
    assert m["graph"][0] < 1.0 and m["gmail"][0] < 1.0, m


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


@pytest.mark.parametrize("html", [
    "<table><tr><td><select><!--</select>--><col>SECRET",
    '<table><tr><td><select title="</select>"><col>SECRET',
    "<table><tr><td><select><script></select></script><col>SECRET",
    "<table><tr><td><select><template></select></template><col>SECRET",
    "<table><tr><td><select><option title='</select>'><col>SECRET",
])
@pytest.mark.parametrize("which", list(SANITIZERS))
def test_a_fake_select_closer_does_not_defeat_the_select_guard(which, html):
    assert "SECRET" not in SANITIZERS[which]("<p>seen</p>" + html)


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


@pytest.mark.parametrize("style", [
    "display:none !important;display:block", "display:none;display:blck", "display:none;display:block\\9",
    "visibility:hidden;visibility:xyz", "display:none ! important", "display:none !IMPORTANT ;display:inline",
    "opacity:0;opacity:1", "font-size:0;font-size:12px",
])
def test_any_hiding_declaration_of_a_property_hides_even_if_a_later_one_would_unhide_it(style):
    assert graph_html.html_to_text(f'<p>seen</p><p style="{style}">SECRET</p>') == "seen"


def test_a_visible_later_value_for_a_property_that_never_hid_still_shows():
    assert "S" in graph_html.html_to_text('<p style="display:block;color:red">S</p>')
    assert "S" in graph_html.html_to_text('<p style="display:inline;display:block">S</p>')


def test_gmail_reports_a_cut_through_body_text():
    import base64

    html = "<div>" * 20_000 + "deep" + "</div>" * 20_000
    data = base64.urlsafe_b64encode(html.encode()).decode()
    payload = {"mimeType": "text/html", "body": {"data": data}}
    text, truncated = gmail_mime.body_text(payload)
    assert truncated is True
    small = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>hi</p>").decode()}}
    assert gmail_mime.body_text(small) == ("hi", False)
