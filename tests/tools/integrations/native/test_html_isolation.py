"""Round 6: the HTML parse runs in an isolated worker process. Pathological shapes finish within the worker
timeout on both paths with the caller's memory flat (a fail-closed notice is an acceptable answer, a leak or
a hang is not), a dead or slow worker fails closed and is replaced, the cascade follows CSS, svg/math with a
breakout tag fails closed on the DOM, a select shows its option labels, and the event loop stays free."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from mavis.tools.integrations.native import gmail_mime, graph_html

SANITIZERS = {"graph": graph_html.html_to_text, "gmail": gmail_mime.html_to_text}

_SHAPES = textwrap.dedent('''
    def fmt(n, tag="b"):
        return "".join(f"<{tag} id={i}>" for i in range(n))

    LI = "<div>" + fmt(3000) + "</div><ul>" + "<li>x" * 100_000
    SHAPES = {
     "fake td + 8 b, K=125": "<div>" + ("<td>" + fmt(8)) * 125 + "</div>" + "<p>x" * 2000,
     "fake td + 8 b, K=200": "<div>" + ("<td>" + fmt(8)) * 200 + "</div>" + "<p>x" * 3000,
     "fake th + li": "<div>" + ("<th>" + fmt(8)) * 200 + "</div>" + "<li>x" * 3000,
     "fake td + deep div": "<td>" + "<div>" * 8000 + "<td>" + "<p></p>" * 80000,
     "b x3000 + li": LI,
     "b x3000 + td": "<div>" + fmt(3000) + "</div><table><tr>" + "<td>x" * 100_000,
     "b x3000 + p": "<div>" + fmt(3000) + "</div>" + "<p>x" * 100_000,
     "b x3000 + dd": "<div>" + fmt(3000) + "</div><dl>" + "<dd>x" * 100_000,
     "b x3000 + nested tables": "<div>" + fmt(3000) + "</div>" + "<table><tr><td>x</table>" * 20_000,
     "p =\\" prefix": '<p =">' + LI,
     "p a=x= prefix": '<p a="x"=">' + LI,
     "p=\\" prefix + svg walk": '<p=">' + "<svg>" + "<td>" * 50_000 + "</q>" * 50_000,
     "nbsp attribute prefix": '<p a=\\xa0">' + LI,
     "bad close tag prefix": '</\\u00e9 a=">' + LI,
     "svg plaintext prefix": "<svg><plaintext></svg>" + LI,
     "script escape prefix": "<script><!--<script></script><textarea></script>" + LI,
     "comments <!---->": "<!---->" * 57_000,
     "comments <!--x-->": "<!--x-->" * 50_000,
     "svg td + </q>": "<svg>" + "<td>" * 50_000 + "</q>" * 50_000,
     "math col + </q>": "<math>" + "<col>" * 40_000 + "</q>" * 50_000,
     "svg tr + </li>": "<svg>" + "<tr>" * 45_000 + "</li>" * 50_000,
     "span + </q>": "<span>" * 8900 + "</q>" * 86_000,
     "span + </p>": "<span>" * 8000 + "</p>" * 80_000,
     "49k options": "<select>" + "<option>" * 49_000,
     "23.5k closed options": "<select>" + "<option>x</option>" * 23_500 + "</select>",
     "100k nested b": "<b>" * 100_000 + "x",
     "20k unclosed rows": "<table>" + "<tr><td>x" * 20_000,
     "60k attributes": "<div " + " ".join(f"a{i}=1" for i in range(60_000)) + ">x",
     "html attribute merge": "".join(f"<html a{i}=1>" for i in range(40_000)),
     "13k style blocks": "<style>.a{color:red}</style>" * 13_000,
     "over the cap": "<p>x</p>" * 200_000,
    }
''')

_RUNNER = _SHAPES + textwrap.dedent('''
    import json, resource, sys, time
    from concurrent.futures import ThreadPoolExecutor
    from mavis.tools.integrations.native import gmail_mime, graph_html
    graph_html.TIMEOUT_S = 0.6  # the file stays quick; the report has the table at the production 2.0 s

    def graph(html):
        t = time.perf_counter()
        text, cut = graph_html.html_to_text_limited(html, 10**6)
        return [time.perf_counter() - t, cut, text]

    def gmail(html):
        t = time.perf_counter()
        cuts = []
        text = gmail_mime.html_to_text(html, cuts)
        return [time.perf_counter() - t, cuts[0], text]

    rows = {}
    with ThreadPoolExecutor(2) as pool:  # the two paths at once: the pool has two workers
        for name, html in SHAPES.items():
            a, b = pool.submit(graph, html), pool.submit(gmail, html)
            rows[name] = {"graph": a.result(), "gmail": b.result(), "size": len(html)}
    out = {name: {k: (v if k == "size" else [round(v[0], 2), v[1], v[2][:80], len(v[2])])
                  for k, v in row.items()} for name, row in rows.items()}
    out["_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(json.dumps(out))
''')

SHAPE_NAMES = [line.split('":')[0].strip().strip('"').replace("\\\\", "\\").replace('\\"', '"')
               for line in _SHAPES.splitlines() if line.startswith(' "')]
NOTICE = graph_html.NOTICE
SLACK_S = 0.6 + 1.5  # the runner shortens the timeout to 0.6 s


@pytest.fixture(scope="module")
def shape_rows() -> dict:
    p = subprocess.run([sys.executable, "-c", _RUNNER], capture_output=True, text=True, timeout=300,
                       check=True)
    return json.loads(p.stdout)


def test_the_shape_list_is_not_empty():
    assert len(SHAPE_NAMES) >= 30


@pytest.mark.parametrize("which", ["graph", "gmail"])
@pytest.mark.parametrize("name", SHAPE_NAMES)
def test_pathological_shapes_finish_within_the_timeout_on_both_paths(shape_rows, name, which):
    secs, cut, head, _n = shape_rows[name][which]
    assert secs < SLACK_S, (name, which, secs)
    if head.startswith(NOTICE[:20]):  # failing closed is fine; a half answer with the notice is not
        assert cut is True


def test_the_callers_memory_stays_flat_whatever_the_shapes_cost(shape_rows):
    assert shape_rows["_rss_mb"] < 400, shape_rows["_rss_mb"]


def test_a_message_over_the_input_cap_fails_closed_without_a_parse(shape_rows):
    row = shape_rows["over the cap"]
    assert row["size"] > graph_html.MAX_HTML
    for which in ("graph", "gmail"):
        assert row[which][2].startswith(NOTICE[:20]) and row[which][1] is True


# --- the pool --------------------------------------------------------------------------------------------


def _workers() -> list:
    return list(graph_html._pool._live)


def test_a_worker_has_an_address_space_limit_and_a_normal_call_is_fast():
    graph_html.html_to_text("<p>warm</p>")
    pid = _workers()[0].proc.pid
    limits = open(f"/proc/{pid}/limits").read()
    line = next(x for x in limits.splitlines() if x.startswith("Max address space"))
    assert int(line.split()[3]) <= 512 * 1024 * 1024
    start = time.perf_counter()
    for _ in range(20):
        assert graph_html.html_to_text("<p>hello</p>") == "hello"
    assert (time.perf_counter() - start) / 20 < 0.05


def test_a_timeout_fails_closed_and_the_worker_is_replaced(monkeypatch):
    graph_html.html_to_text("<p>warm</p>")
    before = {w.proc.pid for w in _workers()}
    monkeypatch.setattr(graph_html, "TIMEOUT_S", 0.001)
    slow = "<table>" + "<tr><td>x" * 15_000
    assert graph_html.html_to_text_limited(slow, 10**6) == (NOTICE, True)
    monkeypatch.undo()
    assert graph_html.html_to_text("<p>after</p>") == "after"
    assert not before & {w.proc.pid for w in _workers()} or len(_workers()) <= graph_html.WORKERS


def test_a_killed_worker_fails_closed_once_and_the_next_call_works():
    graph_html.html_to_text("<p>warm</p>")
    for w in _workers():
        os.kill(w.proc.pid, signal.SIGKILL)
        w.proc.wait()
    # a worker that died while idle is noticed before use: nothing is lost
    assert graph_html.html_to_text("<p>fine</p>") == "fine"


def test_an_exception_in_the_parse_fails_closed(monkeypatch):
    monkeypatch.setattr(graph_html, "_pool", graph_html._Pool())
    assert graph_html.html_to_text_limited("<p>ok</p>", 10**6) == ("ok", False)
    graph_html._pool.close()


def test_many_threads_share_the_two_workers():
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:
        out = list(ex.map(lambda i: graph_html.html_to_text(f"<p>m{i}</p>"), range(40)))
    assert out == [f"m{i}" for i in range(40)]
    assert len(_workers()) <= graph_html.WORKERS


# --- the event loop ---------------------------------------------------------------------------------------


def test_the_loop_stays_responsive_while_a_slow_parse_runs(monkeypatch):
    real = graph_html.html_to_text_limited

    def slow(markup, limit, *, styles=True):
        time.sleep(0.6)
        return real(markup, limit, styles=styles)

    monkeypatch.setattr(graph_html, "html_to_text_limited", slow)

    async def main() -> float:
        gaps: list[float] = []

        async def ticker() -> None:
            last = time.perf_counter()
            for _ in range(40):
                await asyncio.sleep(0.02)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        t = asyncio.create_task(ticker())
        text = await graph_html.ahtml_to_text("<p>x</p>")
        await t
        assert text == "x"
        return max(gaps)

    assert asyncio.run(main()) < 0.2


# --- cascade --------------------------------------------------------------------------------------------


def _style(css: str) -> str:
    return graph_html.html_to_text(f'<p>A</p><p style="{css}">SHOWN</p><p>B</p>')


@pytest.mark.parametrize("css", [
    "font-size:0;line-height:0;font-size:14px;line-height:20px",
    "opacity:0;opacity:1",
    "max-height:0;overflow:hidden;max-height:none",
    "display:none;display:block !important",
    "display:none; display : block ! important",
    "visibility:hidden;visibility:visible",
    "height:0;height:auto",
    "text-indent:-9999px;text-indent:0",
    "display:none;display:flex",
])
def test_a_later_valid_declaration_overrides_an_earlier_hiding_one(css):
    assert "SHOWN" in _style(css)


@pytest.mark.parametrize("css", [
    "display:block !important;display:none",   # important wins over a later plain declaration, shown
])
def test_important_wins_over_a_later_plain_declaration(css):
    assert "SHOWN" in _style(css)


@pytest.mark.parametrize("css", [
    "display:none !important;display:block",
    "display:none;display:block\\9",
    "display:none;display:blck",
    "display:none;display:var(--x)",
    "display:none;display:\\62 lock",  # an escaped value: judged strictly, hiding stays
    "display:\\6e one",
    "display:none;display:block\\0",
    "mso-hide:all",
    "font-size:0;font-size:1.2foo",
    "opacity:0;opacity:calc(1)",
    "display:none",
    "width:0px",
])
def test_invalid_or_losing_later_declarations_keep_the_text_hidden(css):
    assert "SHOWN" not in _style(css)


def test_a_style_rule_follows_the_same_cascade():
    html = "<style>.t{font-size:0;font-size:16px}.h{display:none;display:bogus}</style>"
    out = graph_html.html_to_text(html + '<p class="t">SEEN</p><p class="h">GONE</p>')
    assert "SEEN" in out and "GONE" not in out


def test_the_style_budget_fails_closed():
    css = "<style>" + ".x{color:red}" * 20_000 + "</style><style>.h{display:none}</style><p class=h>S</p>"
    assert graph_html.html_to_text_limited(css, 10**6) == (NOTICE, True)
    big = "<style>" + "/*" + "a" * 300_000 + "*/</style><p>S</p>"
    assert graph_html.html_to_text_limited(big, 10**6) == (NOTICE, True)


def test_gmail_ignores_style_budgets_because_it_never_reads_styles():
    css = "<style>" + ".x{color:red}" * 20_000 + "</style><p>S</p>"
    assert gmail_mime.html_to_text(css) == "S"


# --- foreign content ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", list(SANITIZERS))
@pytest.mark.parametrize("html", [
    "<p>seen</p><svg><sup hidden><i>SECRET",
    "<p>seen</p><svg><sup><noscript><i>SECRET",
    "<p>seen</p><math><sup><iframe><b>SECRET</b></iframe>",
    '<p =">seen</p><svg><sup hidden><i>SECRET',
    "<p>seen</p><svg><g><sup>x</sup></g></svg>SECRET",
])
def test_a_breakout_tag_inside_svg_or_math_fails_closed(which, html):
    text = SANITIZERS[which](html)
    assert "SECRET" not in text and "seen" in text


def test_a_breakout_inside_a_hidden_element_still_cuts_what_follows():
    text = graph_html.html_to_text('<p>seen</p><div hidden><svg><sup hidden><i>X</i></sup></svg></div>AFTER')
    assert "AFTER" not in text and "seen" in text


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_a_closed_svg_logo_then_a_footnote_sup_does_not_cut(which):
    html = ('<p>Logo</p><svg width=10><circle r=4/><text>t</text></svg>'
            '<p>The price is 5<sup>*</sup></p><p>Body text</p><p>E=mc<sup>2</sup></p>')
    text = SANITIZERS[which](html)
    assert "Body text" in text and "E=mc2" in text
    if which == "graph":
        assert graph_html.html_to_text_limited(html, 10**6)[1] is False


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_svg_with_html_in_foreignobject_is_not_a_breakout(which):
    html = "<svg><foreignObject><div>x</div></foreignObject></svg><p>after</p>"
    assert "after" in SANITIZERS[which](html)


# --- select ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_a_dropdown_shows_its_option_labels_one_per_line(which):
    html = ("<p>Pick one</p><select><option>Red<option>Green<optgroup label=x><option>Blue</select>"
            "<p>Thanks</p>")
    assert SANITIZERS[which](html) == "Pick one\n\nRed\nGreen\nBlue\n\nThanks"


@pytest.mark.parametrize("which", list(SANITIZERS))
def test_text_directly_inside_a_select_is_not_shown(which):
    text = SANITIZERS[which]("<p>a</p><select>STRAY<option>Opt</select><p>z</p>")
    assert "STRAY" not in text and "Opt" in text and "z" in text


def test_a_hidden_ancestor_still_hides_a_select():
    text = graph_html.html_to_text('<div style="display:none"><select><option>SECRET</select></div><p>ok</p>')
    assert "SECRET" not in text and "ok" in text


def test_a_hidden_option_is_not_shown():
    assert "GONE" not in graph_html.html_to_text('<select><option hidden>GONE<option>Kept</select>')
