"""Crude content terms of a text, and whether a model-written text comes from the user's own words.

`terms` is the overlap vocabulary of tool selection (registry.select). `grounded_in` is the provenance
test for self-only actions (track 1, T1.1): an argument the model wrote is the user's own request only
when every content term of it is a word the user wrote this turn, and every identifier in it (a URL, a
host, an email address, a handle) appears in the user's words verbatim. It is a measured
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
_PREFIX = 5  # "comparison" ~ "compar(e)": a stem of at least this length that begins the other term


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
    return len(term) >= _PREFIX and any(
        len(t) >= _PREFIX and (term.startswith(t) or t.startswith(term)) for t in theirs)


def identifiers(text: str) -> set[str]:
    return {m.group(0).rstrip(".,;:!?)").lower() for m in _IDENTIFIER.finditer(text or "")}


def foreign_terms(text: str, user_words: str) -> set[str]:
    """Content terms of `text` the user did not write (stems of the same word count as written)."""
    theirs = terms(user_words)
    return {t for t in terms(text) if not _same(t, theirs)}


def grounded_in(text: str, user_words: str) -> bool:
    """`text` (written by the model) says nothing the user did not say in `user_words`.

    Every content term of it (words and numbers, minus stopwords) is one the user wrote, and every
    identifier in it (URL, host, email address, handle) appears in their words verbatim. Reordering, dropping
    words and inflection are fine ("compare laptops" ~ "laptop comparison"); a single added content word
    ("budget", "forward", "invoices", a number) is not: the text is then not purely theirs."""
    if not text.strip() or not user_words.strip() or not terms(text):
        return False
    said = user_words.lower()
    if any(ident not in said for ident in identifiers(text)):
        return False
    return not foreign_terms(text, user_words)
