"""Crude content terms of a text, and whether a model-written text comes from the user's own words.

`terms` is the overlap vocabulary of tool selection (registry.select). `grounded_in` is the provenance
test for self-only actions (track 1, T1.1): an argument the model wrote is the user's own request when
its content terms are, for the most part, words the user wrote this turn, and every identifier in it
(a URL, a host, an email address, a handle) appears in the user's words verbatim. It is a measured
overlap, not a phrase list: it never decides what an action is about, only where its wording came from.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that say nothing about what a text is about ("the", "user", "a"): ignored by every overlap score.
STOPWORDS = frozenset(
    "a an and are as at be by can do for from i in is it me my of on or s so the their them they this "
    "to up us user we what when with you your".split()
)
_SUFFIXES = ("ings", "ing", "ers", "er", "ed", "es", "s")
# Identifiers a planted text would smuggle in: they must be the user's, character for character.
_IDENTIFIER = re.compile(
    r"https?://\S+|www\.\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|@\w{2,}|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b",
    re.IGNORECASE,
)
GROUNDED_SHARE = 0.6  # at least this share of the text's content terms must be the user's
_PREFIX = 5  # "comparison" ~ "compare": two long terms with the same first letters are one word


def terms(text: str) -> set[str]:
    """Crude stems: "reminder" ~ "remind", "emails" ~ "email"."""
    out = set()
    for word in _WORD_RE.findall(text.lower()):
        if word in STOPWORDS:
            continue
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
        out.add(word[:-1] if word.endswith("e") and len(word) > 3 else word)
    return out


def _same(term: str, theirs: set[str]) -> bool:
    if term in theirs:
        return True
    return len(term) >= _PREFIX and any(len(t) >= _PREFIX and t[:_PREFIX] == term[:_PREFIX] for t in theirs)


def identifiers(text: str) -> set[str]:
    return {m.group(0).rstrip(".,;:!?)").lower() for m in _IDENTIFIER.finditer(text or "")}


def grounded_in(text: str, user_words: str) -> bool:
    """`text` (written by the model) is drawn from `user_words` (written by the user)."""
    if not text.strip() or not user_words.strip():
        return False
    said = user_words.lower()
    if any(ident not in said for ident in identifiers(text)):
        return False
    mine = terms(text)
    if not mine:
        return False
    theirs = terms(user_words)
    shared = sum(1 for t in mine if _same(t, theirs))
    return shared / len(mine) >= GROUNDED_SHARE
