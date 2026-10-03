"""Counterparty keys for money baselines: deterministic normalization plus stdlib difflib matching.

No embeddings (names are not semantics) and no new dependency (the per-user key set is tiny). The key is
also the only counterparty text Mavis ever shows: it has no dots, no '@' and no long digit runs."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

MAX_KEY = 60
FUZZY_CUTOFF = 0.88
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
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


def normalize_counterparty(raw: str) -> str:
    s = str(raw or "").casefold().strip()
    if "@" in s:  # payment handles and addresses: the part before @ names the party
        s = s.split("@", 1)[0]
    words = [w for w in _NON_ALNUM.sub(" ", s).split() if not (w.isdigit() and len(w) >= 4)]
    while words and words[0] in LEADING:
        words.pop(0)
    while len(words) > 1 and words[-1] in TRAILING:
        words.pop()
    return " ".join(words)[:MAX_KEY].strip()


def match_key(key: str, known: Iterable[str], cutoff: float = FUZZY_CUTOFF) -> str:
    if not key:
        return ""
    pool = [k for k in known if k]
    if key in pool:
        return key
    close = difflib.get_close_matches(key, pool, n=1, cutoff=cutoff)
    return close[0] if close else key


def display_name(key: str) -> str:
    return key.title()[:40] if key else "an unknown payee"
