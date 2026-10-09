"""Markdown to Docs batchUpdate requests: text, ranges (UTF-16), headings, lists, inline styles."""

from __future__ import annotations

import time

import pytest

from mavis.tools.integrations.native.docs_markdown import markdown_requests, parse_inline, utf16_len


def split(requests: list[dict]):
    text = requests[0]["insertText"]["text"]
    assert requests[0]["insertText"]["location"] == {"index": 1}
    kinds = [next(iter(r)) for r in requests[1:]]
    return text, requests[1:], kinds


def covered(text: str, request: dict) -> str:
    """The text a styling request's range covers (index 1 is the first character)."""
    r = next(iter(request.values()))["range"]
    units = text.encode("utf-16-le")
    return units[(r["startIndex"] - 1) * 2 : (r["endIndex"] - 1) * 2].decode("utf-16-le")


def test_blank_markdown_makes_no_requests():
    assert markdown_requests("") == [] and markdown_requests("  \n\n") == []


def test_plain_paragraph_is_one_insert_without_a_trailing_newline():
    reqs = markdown_requests("Hello world")
    assert reqs == [{"insertText": {"location": {"index": 1}, "text": "Hello world"}}]


def test_headings_get_named_styles_over_their_paragraph():
    text, styles, kinds = split(markdown_requests("# Title\n\n## Part ##\nbody"))
    assert text == "Title\nPart\nbody"
    assert kinds == ["updateParagraphStyle", "updateParagraphStyle"]
    h1, h2 = (s["updateParagraphStyle"] for s in styles)
    assert h1["paragraphStyle"] == {"namedStyleType": "HEADING_1"} and h1["fields"] == "namedStyleType"
    assert h1["range"] == {"startIndex": 1, "endIndex": 7}
    assert h2["paragraphStyle"]["namedStyleType"] == "HEADING_2"
    assert h2["range"] == {"startIndex": 7, "endIndex": 12}


def test_bold_italic_code_strike_and_link_ranges():
    text, styles, _ = split(markdown_requests("a **bold** and *it* and `x = 1` ~~old~~ [site](https://e.com/p)"))
    assert text == "a bold and it and x = 1 old site"
    by_field = {s["updateTextStyle"]["fields"]: s["updateTextStyle"] for s in styles}
    assert covered(text, {"u": by_field["bold"]}) == "bold"
    assert by_field["bold"]["textStyle"] == {"bold": True}
    assert covered(text, {"u": by_field["italic"]}) == "it"
    assert covered(text, {"u": by_field["weightedFontFamily"]}) == "x = 1"
    assert by_field["weightedFontFamily"]["textStyle"]["weightedFontFamily"]["fontFamily"] == "Courier New"
    assert covered(text, {"u": by_field["strikethrough"]}) == "old"
    assert covered(text, {"u": by_field["link"]}) == "site"
    assert by_field["link"]["textStyle"] == {"link": {"url": "https://e.com/p"}}


def test_nested_inline_styles_and_triple_markers():
    text, styles, _ = split(markdown_requests("**bold with *both* inside** and ***all***"))
    assert text == "bold with both inside and all"
    got = {(covered(text, {"u": s["updateTextStyle"]}), s["updateTextStyle"]["fields"]) for s in styles}
    assert ("bold with both inside", "bold") in got and ("both", "italic") in got
    assert ("all", "bold,italic") in got


def test_unmatched_markers_and_escapes_stay_literal():
    assert parse_inline("2 * 3 and snake_case_name") == ("2 * 3 and snake_case_name", [])
    assert parse_inline(r"\*not italic\*") == ("*not italic*", [])


def test_unsafe_link_scheme_is_not_a_link():
    plain, spans = parse_inline("[click](javascript:alert(1)) and [ok](mailto:a@b.co)")
    assert plain.startswith("click (javascript:alert(1")  # shown, never linked
    assert [s[2] for s in spans] == [{"link": {"url": "mailto:a@b.co"}}]


def test_bullets_numbers_and_nesting_use_tabs_and_run_last_in_reverse():
    md = "intro\n- one\n  - nested\n- two\n\n1. first\n2. second"
    text, rest, kinds = split(markdown_requests(md))
    assert text == "intro\none\n\tnested\ntwo\nfirst\nsecond"
    assert kinds == ["createParagraphBullets", "createParagraphBullets"]
    numbered, bullets = (r["createParagraphBullets"] for r in rest)  # last list first
    assert numbered["bulletPreset"] == "NUMBERED_DECIMAL_ALPHA_ROMAN"
    assert bullets["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE"
    assert bullets["range"]["startIndex"] == 7
    assert numbered["range"]["startIndex"] >= bullets["range"]["endIndex"]
    assert covered(text, {"u": {"range": bullets["range"]}}) == "one\n\tnested\ntwo\n"


def test_inline_styles_inside_a_nested_bullet_account_for_the_tab():
    text, styles, kinds = split(markdown_requests("- a\n  - **b**"))
    assert text == "a\n\tb"
    bold = next(s for s in styles if "updateTextStyle" in s)
    assert covered(text, bold) == "b"
    assert kinds[-1] == "createParagraphBullets"  # bullets come after every text style


def test_code_block_is_monospace_and_not_parsed():
    text, styles, _ = split(markdown_requests("```py\nx = **1**\n\n  y\n```\nafter"))
    assert text == "x = **1**\n\n  y\nafter"
    mono = [s for s in styles if "updateTextStyle" in s]
    assert [covered(text, m) for m in mono] == ["x = **1**", "  y"]


def test_table_rule_and_quote():
    text, styles, _ = split(markdown_requests("| a | b |\n|---|---|\n| 1 | 2 |\n---\n> quoted"))
    assert text == "a | b\n1 | 2\nquoted"
    quote = styles[-1]["updateParagraphStyle"]
    assert quote["fields"] == "indentStart" and covered(text, styles[-1]) == "quoted"


def test_blank_line_between_paragraphs_is_kept_once():
    assert markdown_requests("a\n\n\n\nb")[0]["insertText"]["text"] == "a\n\nb"


def test_ranges_count_utf16_units_not_characters():
    text, styles, _ = split(markdown_requests("😀 **x**"))
    assert utf16_len("😀") == 2
    assert styles[0]["updateTextStyle"]["range"] == {"startIndex": 4, "endIndex": 5}
    assert covered(text, styles[0]) == "x"


PATHOLOGICAL = {
    "brackets": "[" * 100_000,
    "stars": "*" * 100_000,
    "ticks": "`" * 100_000,
    "unders": "_" * 100_000,
    "tildes": "~" * 100_000,
    "open_links": "[a](" * 25_000,
    "nested_links": "[" * 30_000 + "x" + "](u)" * 20_000,
    "star_words": "*a " * 33_000,
    "bracket_words": "[a " * 33_000,
    "mixed": "**_[`~~" * 14_000,
}


@pytest.mark.parametrize("name", sorted(PATHOLOGICAL))
def test_inline_parsing_is_linear_on_pathological_input(name):
    start = time.perf_counter()
    markdown_requests(PATHOLOGICAL[name])
    assert time.perf_counter() - start < 1.0
