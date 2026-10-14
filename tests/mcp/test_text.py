"""Text hygiene for server-written text (adapted from Hermes Agent)."""

from __future__ import annotations

import pytest

from mavis.mcp import text as t

TAG = "".join(chr(0xE0000 + ord(c)) for c in "ignore")


def test_unicode_tag_characters_are_stripped() -> None:
    assert t.strip_unicode_tags(f"hello{TAG} world") == "hello world"


def test_a_flag_emoji_tag_sequence_survives() -> None:
    scotland = "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in "gbsct") + "\U000E007F"
    assert t.strip_unicode_tags(f"a{scotland}b") == f"a{scotland}b"


def test_ansi_sequences_are_removed() -> None:
    assert t.strip_ansi("\x1b[31mred\x1b[0m \x1b]0;title\x07done") == "red done"
    assert t.strip_ansi("plain") == "plain"


def test_clean_text_removes_controls_and_invisible_characters_but_keeps_layout() -> None:
    raw = f"a\x00b\x07c‮evil​x﻿\tq\nr\r\nz\rend{TAG}\x1b[1m"
    assert t.clean_text(raw) == "abcevilx\tq\nr\nz\nend"


def test_the_zero_width_joiner_stays_for_emoji() -> None:
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    assert t.clean_text(family) == family


def test_clean_description_collapses_whitespace_and_truncates_to_the_limit() -> None:
    assert t.clean_description("  Find   things\n\nfast\t ") == "Find things fast"
    long = t.clean_description("word " * 400, limit=600)
    assert len(long) <= 600 and long.endswith("...")
    assert t.clean_description(None) == "" and t.clean_description(5) == ""


@pytest.mark.parametrize("secret", [
    "ghp_" + "a" * 36, "sk-proj-abc123DEF456ghi789", "Bearer abc.def.ghi", "token=abc123def456",
    "api_key=SECRETVALUE", "password=hunter2hunter2", "xoxb-1234567890-abcdefghij",
    "AKIAABCDEFGHIJKLMNOP", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl",
])
def test_credential_shapes_are_redacted(secret: str) -> None:
    out = t.sanitize_error(f"failed calling https://x.example/y with {secret} and more")
    assert secret not in out and t.REDACTED in out


def test_a_secret_we_hold_is_redacted_even_when_it_has_no_known_shape() -> None:
    out = t.sanitize_error("upstream said: bad token Zq9xLm2PabCd", secrets=["Zq9xLm2PabCd", "", "abc"])
    assert "Zq9xLm2PabCd" not in out
    assert t.sanitize_error("abc stays", secrets=["abc"]) == "abc stays"  # too short to be a secret


def test_text_that_fits_is_not_truncated() -> None:
    assert t.truncate_head_tail("short", 100) == "short"


def test_truncation_keeps_a_head_and_a_longer_tail_inside_the_budget() -> None:
    text = "H" * 5000 + "M" * 20000 + "T" * 5000
    out = t.truncate_head_tail(text, 12000)
    assert len(out) <= 12000
    assert out.startswith("H" * 4000) and out.endswith("T" * 4000)
    assert "OUTPUT TRUNCATED" in out and "out of 30,000 total" in out
    head = out.split("\n\n... [")[0]
    tail = out.split("] ...\n\n")[1]
    assert 0.35 < len(head) / (len(head) + len(tail)) < 0.45


def test_a_budget_smaller_than_the_marker_still_yields_the_marker() -> None:
    out = t.truncate_head_tail("x" * 1000, 10)
    assert "TRUNCATED" in out
