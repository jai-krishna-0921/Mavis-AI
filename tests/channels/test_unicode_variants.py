"""Track 1 T1.4 (hotfix4 H6): the one output pass maps Unicode space and hyphen variants by property.

Every space separator (category Zs) becomes a plain space, word-joining hyphens (U+2010, U+2011) become
"-", invisible break hints are removed; inline code, URLs and verbatim spans are never touched."""

from __future__ import annotations

import unicodedata

import pytest

from mavis.channels.formatting import normalize_variants, to_plain, to_telegram_html, verbatim

ALL_ZS = [chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Zs" and chr(c) != " "]


@pytest.mark.parametrize("space", ALL_ZS)
def test_every_space_separator_becomes_a_plain_space(space):
    assert to_plain(f"Mon{space}05{space}Oct at 2{space}pm") == "Mon 05 Oct at 2 pm"


@pytest.mark.parametrize("raw,expected", [
    ("e‑mail me", "e-mail me"),
    ("a well‐known spot", "a well-known spot"),
    ("“non‑breaking” words", "“non-breaking” words"),  # in quotes too: invisible
    ("co­operate", "cooperate"),
    ("zero​width and⁠joiner and﻿bom", "zerowidth andjoiner andbom"),
    ("2 pm‑to 3 pm", "2 pm-to 3 pm"),
    ("Pune — rain later", "Pune, rain later"),
])
def test_hyphen_and_invisible_variants(raw, expected):
    assert to_plain(raw) == expected
    assert to_telegram_html(raw) == expected


@pytest.mark.parametrize("raw", [
    "family \U0001F468‍\U0001F469‍\U0001F467 time",  # zero-width joiners build the emoji
    "it was −5 degrees",  # a minus sign is a sign, not a hyphen
    "plain ascii - stays -- as is",
])
def test_what_must_stay(raw):
    assert to_plain(raw) == raw


def test_code_urls_and_verbatim_spans_are_untouched():
    assert normalize_variants("run `a b` now") == "run `a b` now"
    assert to_telegram_html("see `x‑y`") == "see <code>x‑y</code>"
    assert to_plain("go to https://example.com/a‑b now") == "go to https://example.com/a‑b now"
    preview = "Email: Hi Ravi, see you"
    assert to_plain("Card:\n" + verbatim(preview)) == "Card:\n" + preview  # the card equals the action


def test_a_range_loses_its_thin_spacing_with_the_dash():
    assert to_plain("Free 15:00 – 16:00") == "Free 15:00-16:00"
    assert to_plain("Pages 3 – 5") == "Pages 3-5"
