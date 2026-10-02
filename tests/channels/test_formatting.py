import re

import pytest

from mavis.channels.formatting import sanitize_typography, to_plain, to_telegram_html

H = to_telegram_html


def test_bold() -> None:
    assert H("a **b** c") == "a <b>b</b> c"
    assert H("a __b__ c") == "a <b>b</b> c"


def test_italic_not_in_words() -> None:
    assert H("an *x* here") == "an <i>x</i> here"
    assert H("so _x_ here") == "so <i>x</i> here"
    assert H("use snake_case_name now") == "use snake_case_name now"
    assert H("2*3*4") == "2*3*4"


def test_inline_code_has_no_inner_conversion() -> None:
    assert H("run `a **b** <c>`") == "run <code>a **b** &lt;c&gt;</code>"


def test_fenced_block_escaped() -> None:
    out = H("x\n```py\nif a < b & **c**:\n```\ny")
    assert out == "x\n<pre>if a &lt; b &amp; **c**:</pre>\ny"


def test_links() -> None:
    assert H("see [docs](https://e.com/a?b=1&c=2)") == 'see <a href="https://e.com/a?b=1&amp;c=2">docs</a>'
    assert '&quot;' in H('[x](https://e.com/"q)')


def test_headings_bullets_numbers() -> None:
    assert H("# Title\n## Sub") == "<b>Title</b>\n<b>Sub</b>"
    assert H("- a\n* b\n+ c") == "• a\n• b\n• c"
    assert H("1. one\n2. two") == "1. one\n2. two"
    assert H("**bold** start") == "<b>bold</b> start"


def test_table_and_hr() -> None:
    md = "| Name | Age |\n|------|-----|\n| Jai | 30 |\n\n---\n\nend"
    assert H(md) == "Name | Age\nJai | 30\n\nend"


def test_html_chars_escaped() -> None:
    assert H("a <b> & c") == "a &lt;b&gt; &amp; c"


@pytest.mark.parametrize(
    "md",
    ["**bold", "*a **b* c**", "__x_", "`open", "```\nopen block", "[a](http://x", "**a *b** c*"],
)
def test_unbalanced_markers_stay_valid(md: str) -> None:
    out = H(md)
    stack = []
    for m in re.finditer(r"<(/?)(\w+)", out):
        if m.group(1):
            assert stack.pop() == m.group(2)
        else:
            stack.append(m.group(2))
    assert not stack


def test_unclosed_bold_is_literal() -> None:
    assert H("**bold") == "**bold"


def test_plain_strips_markup() -> None:
    md = "# Hi\n**b** and *i* and `c` [l](https://x.io)\n- item\n| a | b |"
    assert to_plain(md) == "Hi\nb and i and c l (https://x.io)\n• item\na | b"
    assert to_plain("```\n<x>\n```") == "<x>"


def test_dashes() -> None:
    assert sanitize_typography("assistant—think") == "assistant, think"
    assert sanitize_typography("a — b") == "a, b"
    assert sanitize_typography("pages 3–5") == "pages 3-5"
    assert sanitize_typography("fast – very fast") == "fast, very fast"
    assert sanitize_typography("wait —, what") == "wait, what"
    assert sanitize_typography("end. — next") == "end. next"


@pytest.mark.parametrize("fn", [H, to_plain])
def test_no_dashes_survive(fn) -> None:
    out = fn("**x**—y\n# a – b\n1–2 [l—m](https://x.io)")
    assert "—" not in out and "–" not in out


@pytest.mark.parametrize("fn", [H, to_plain])
def test_dashes_kept_in_code(fn) -> None:
    out = fn("a—b `c—d`\n```\ne — f\n```")
    assert "c—d" in out and "e — f" in out and "a, b" in out


def test_link_text_with_code() -> None:
    out = H("see [`foo`](https://x.io) now")
    assert out == 'see <a href="https://x.io"><code>foo</code></a> now'
    assert "\x00" not in out


def test_heading_keeps_trailing_hash() -> None:
    assert H("# C#") == "<b>C#</b>"
    assert to_plain("## F# rocks") == "F# rocks"


def test_no_italic_inside_bare_url() -> None:
    url = "https://x.io/a_b_c/*d*"
    assert H(f"go {url} ok") == f"go {url} ok"
    assert to_plain(f"go {url} ok") == f"go {url} ok"
