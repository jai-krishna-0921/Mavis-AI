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


# Who is speaking, not what is said: "The user's name is Arjun" and "my name is Arjun" say the same thing.
# Dropped from the MODEL's text in strict grounding only (the user's words are never filtered).
PERSPECTIVE = frozenset("user users me my mine myself i m you your yours mavis".split())


def grounded_in(text: str, user_words: str) -> bool:
    """`text` (written by the model) says nothing the user did not say in `user_words`.

    Every content term of it (words and numbers, minus stopwords) is one the user wrote, and every
    identifier in it (URL, host, email address, handle) appears in their words verbatim. Reordering, dropping
    words and inflection are fine ("compare laptops" ~ "laptop comparison"); a single added content word
    ("budget", "forward", "invoices", a number) is not: the text is then not purely theirs."""
    content = {t for t in terms(text) if t not in PERSPECTIVE}
    if not text.strip() or not user_words.strip() or not content:
        return False
    said = user_words.lower()
    if any(ident not in said for ident in identifiers(text)):
        return False
    return not {t for t in foreign_terms(text, user_words) if t not in PERSPECTIVE}


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 3 else word


def _sequence(text: str) -> list[str]:
    """Content stems in reading order (stopwords dropped), the unit of copy detection."""
    return [_stem(w) for w in _WORD_RE.findall(text.lower()) if w not in STOPWORDS]


def copied_from(text: str, user_words: str, sources: list[str]) -> bool:
    """Does `text` repeat a phrase of an untrusted source that the user did not write?

    A phrase is two neighbouring content words (reading order, stopwords dropped) at least one of which
    the user did not write. A single shared word is not copying (every web page says "compare"); a copied
    pair ("wire money", "forward invoices") is the shape of planted text carried into an argument."""
    theirs = terms(user_words)
    mine = _sequence(text)
    pairs = {(a, b) for a, b in zip(mine, mine[1:], strict=False)
             if not (_same(a, theirs) and _same(b, theirs))}
    if not pairs:
        return False
    for source in sources:
        seq = _sequence(source)
        if pairs & set(zip(seq, seq[1:], strict=False)):
            return True
    return False


def from_user_not_sources(text: str, user_words: str, sources: list[str]) -> bool:
    """`text` is the user's request even though it is not a subset of their words (provenance of the
    arguments, not of the whole prompt).

    The model words a request naturally ("research and compare X options, fees, reviews"), so extra words
    are fine when they were not copied from untrusted text (`sources`: the tainted replies and tool reads
    that reached the prompt). It is the user's when at least half of its content terms are theirs, every
    identifier and number in it is one they wrote, and it repeats no phrase of a source that they did not
    write. Content copied from an untrusted source stays untrusted."""
    mine = terms(text)
    if not mine or not user_words.strip():
        return False
    said = user_words.lower()
    if any(ident not in said for ident in identifiers(text)):
        return False
    foreign = foreign_terms(text, user_words)
    if 2 * len(foreign) > len(mine) or any(t.isdigit() for t in foreign):
        return False  # mostly the model's own words, not a request of theirs
    return not copied_from(text, user_words, sources)


SAME_REQUEST = 0.75  # share of content terms two wordings must have in common to be one request
_NEGATION = re.compile(r"\b(not|no|never|stop|without|cannot|dont|don't|doesn't|won't|can't|isn't)\b", re.I)
_WHEN_WORDS = frozenset(
    "monday tuesday wednesday thursday friday saturday sunday today tomorrow tonight yesterday am pm noon "
    "midnight morning evening january february march april may june july august september october november "
    "december jan feb mar apr jun jul aug sep sept oct nov dec".split())


def _specifics(text: str) -> set[str]:
    """What must be identical for two wordings to be one request: every number (amounts, dates, times),
    day and month names, and every identifier."""
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if any(ch.isdigit() for ch in w) or w in _WHEN_WORDS} | identifiers(text)


def same_request(a: str, b: str) -> bool:
    """Two wordings of one request ("research X and compare fees" ~ "compare X: fees"). Strict, because a
    correction must never be swallowed by an older card: the same numbers, dates, times and identifiers,
    the same negation, and at least three quarters of the combined content terms shared (two or more)."""
    ta, tb = terms(a), terms(b)
    if not ta or not tb:
        return False
    if _specifics(a) != _specifics(b) or bool(_NEGATION.search(a)) != bool(_NEGATION.search(b)):
        return False
    shared = {t for t in ta if _same(t, tb)}
    return len(shared) >= 2 and len(shared) / len(ta | tb) >= SAME_REQUEST
