"""Invite codes: 10 Crockford base32 characters (50 bits), shown as MAV-XXXXX-XXXXX, stored as sha256 only."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Literal

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford: no I, L, O, U
LENGTH = 10
_PREFIX = "MAV"
_AMBIGUOUS = str.maketrans({"I": "1", "L": "1", "O": "0"})
_STRIP = re.compile(r"[\s\-_]+")


class InviteError(Exception):
    def __init__(self, reason: Literal["invalid", "limit", "cap"]) -> None:
        super().__init__(reason)
        self.reason = reason


def generate_code() -> str:
    """Always contains a digit, so a spaced-out code can be told apart from ten plain letters."""
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        if any(ch.isdigit() for ch in code):
            return code


def normalize(text: str) -> str | None:
    """The canonical 10-character code in `text`, or None. Case, dashes, spaces and a MAV prefix are
    ignored; I and L read as 1 and O as 0 (Crockford)."""
    raw = (text or "").strip().upper()
    s = _STRIP.sub("", raw)
    prefixed = s.startswith(_PREFIX) and len(s) == LENGTH + len(_PREFIX)
    if prefixed:
        s = s[len(_PREFIX):]
    # Two plain words such as "hello there" must not read as a spaced-out code: separated groups need a
    # digit (generated codes always have one) unless the MAV prefix is present.
    if not prefixed and _STRIP.search(raw) and not any(ch.isdigit() for ch in s):
        return None
    s = s.translate(_AMBIGUOUS)
    if len(s) != LENGTH or any(ch not in ALPHABET for ch in s):
        return None
    return s


_EMBEDDED = re.compile(r"(?<![A-Za-z0-9])MAV[-\s_]?([0-9A-Za-z]{5})[-\s_]?([0-9A-Za-z]{5})(?![A-Za-z0-9])", re.IGNORECASE)


def find_code(text: str) -> str | None:
    """The canonical code written inside a longer message ("my invite is MAV-7K3QZ-9XW2B, thanks"). Only
    the MAV prefixed shape counts, so ordinary words and numbers are never taken for a code."""
    m = _EMBEDDED.search(text or "")
    return normalize("MAV" + m.group(1) + m.group(2)) if m else None


def looks_like_code(text: str) -> bool:
    return normalize(text) is not None or find_code(text) is not None


def display(code: str) -> str:
    return f"{_PREFIX}-{code[:5]}-{code[5:]}"


def deep_link_param(code: str) -> str:
    return f"{_PREFIX}{code}"  # Telegram start parameters allow [A-Za-z0-9_-]


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def hint(code: str) -> str:
    return code[-4:]
