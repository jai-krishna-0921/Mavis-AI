"""Counterparty keys for money baselines: deterministic normalization plus stdlib difflib matching.

No embeddings (names are not semantics) and no new dependency (the per-user key set is tiny). The key is
also the only counterparty text Mavis ever shows: it has no dots, no '@' and no long digit runs."""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Iterable

MAX_KEY = 60
FUZZY_CUTOFF = 0.88
_NON_ALNUM = re.compile(r"[^\w ]+|_")
PHONE_DIGITS = 7  # a run of numeric tokens this long is a phone or account number
MIN_FUZZY = 5
LEADING = frozenset({"to", "from", "by", "mr", "mrs", "ms", "dr", "shri", "smt"})
TRAILING = frozenset(
    {
        "pvt",
        "private",
        "ltd",
        "limited",
        "inc",
        "llc",
        "llp",
        "corp",
        "corporation",
        "co",
        "company",
        "technologies",
        "technology",
        "tech",
        "india",
        "services",
        "payments",
        "retail",
        "online",
        "store",
    }
)


def _fold(raw: str) -> str:
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", str(raw or "")))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


def _drop_numbers(words: list[str]) -> list[str]:
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if sum(map(len, run)) < PHONE_DIGITS:
            out.extend(w for w in run if len(w) < 4)
        run.clear()

    for w in words:
        if w.isdigit():
            run.append(w)
        else:
            flush()
            out.append(w)
    flush()
    return out


def normalize_counterparty(raw: str) -> str:
    s = _fold(raw)
    if "@" in s:  # payment handles and addresses: the part before @ names the party
        s = s.split("@", 1)[0]
    words = _drop_numbers(_NON_ALNUM.sub(" ", s).split())
    while words and words[0] in LEADING:
        words.pop(0)
    while len(words) > 1 and words[-1] in TRAILING:
        words.pop()
    return " ".join(words)[:MAX_KEY].strip()


def match_key(key: str, known: Iterable[str], cutoff: float = FUZZY_CUTOFF) -> str:
    """Exact match, else a typo join for long single-word alphabetic keys only. Payees that differ in a
    trailing token, digit or letter ("seller a" vs "seller b", "flipkart 1" vs "flipkart 2") stay apart."""
    if not key:
        return ""
    pool = [k for k in known if k]
    if key in pool:
        return key
    if " " in key or len(key) < MIN_FUZZY or not key.isalpha():
        return key
    singles = [k for k in pool if " " not in k and len(k) >= MIN_FUZZY and k.isalpha()]
    close = difflib.get_close_matches(key, singles, n=1, cutoff=cutoff)
    return close[0] if close else key


def display_name(key: str) -> str:
    return key.title()[:40] if key else "an unknown payee"
