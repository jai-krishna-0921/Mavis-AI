from __future__ import annotations

import pytest

from mavis.access import codes


def test_generated_codes_are_crockford_and_distinct():
    seen = {codes.generate_code() for _ in range(500)}
    assert len(seen) == 500
    for c in seen:
        assert len(c) == 10 and set(c) <= set(codes.ALPHABET)


@pytest.mark.parametrize("typed", ["MAV-7K3QZ-9XW2B", "mav7k3qz9xw2b", "7K3QZ 9XW2B", "  7k3qz-9xw2b  "])
def test_normalize_accepts_the_shapes_people_send(typed):
    assert codes.normalize(typed) == "7K3QZ9XW2B"


@pytest.mark.parametrize("ambiguous,clean", [("7K3QZ9XWIB", "7K3QZ9XW1B"), ("O0O0O0O0O0", "0000000000"),
                                             ("LLLLLLLLLL", "1111111111")])
def test_normalize_maps_ambiguous_letters(ambiguous, clean):
    assert codes.normalize(ambiguous) == clean


@pytest.mark.parametrize("junk", ["", "hello there", "MAV-123", "7K3QZ9XW2BB7", "UUUUUUUUUU"])
def test_normalize_rejects_non_codes(junk):
    assert codes.normalize(junk) is None


def test_display_hash_hint_and_deep_link():
    c = "7K3QZ9XW2B"
    assert codes.display(c) == "MAV-7K3QZ-9XW2B"
    assert codes.hint(c) == "XW2B"
    assert codes.deep_link_param(c) == "MAV7K3QZ9XW2B"
    assert codes.code_hash(c) == codes.code_hash(codes.normalize("mav-7k3qz-9xw2b"))
    assert len(codes.code_hash(c)) == 64
