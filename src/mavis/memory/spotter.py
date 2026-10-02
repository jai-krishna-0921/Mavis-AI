"""Find which known entities a message mentions, in microseconds, with no LLM.

Aho-Corasick over normalised names + aliases (regex alternation fallback when
pyahocorasick isn't installed). Matches must sit on word boundaries.
"""

from __future__ import annotations

import re
import time
from typing import Protocol

from mavis.domain.memory import Entity
from mavis.memory.names import normalize_name

try:
    import ahocorasick  # type: ignore[import-not-found]

    _HAS_AC = True
except ImportError:  # pragma: no cover - depends on platform wheels
    ahocorasick = None
    _HAS_AC = False


class _EntitySource(Protocol):
    async def entities(self, user_id: int) -> list[Entity]: ...


class EntitySpotter:
    def __init__(self, entities: list[Entity]) -> None:
        self._terms: dict[str, str] = {}
        for e in entities:
            for term in (e.name, *e.aliases):
                n = normalize_name(term)
                if len(n) >= 2:
                    self._terms.setdefault(n, e.name)
        self._ac = None
        self._rx: re.Pattern[str] | None = None
        if not self._terms:
            return
        if _HAS_AC:
            automaton = ahocorasick.Automaton()
            for term, canonical in self._terms.items():
                automaton.add_word(term, (term, canonical))
            automaton.make_automaton()
            self._ac = automaton
        else:
            alternation = "|".join(re.escape(t) for t in sorted(self._terms, key=len, reverse=True))
            self._rx = re.compile(rf"(?<!\S)({alternation})(?!\S)")

    def spot(self, text: str) -> list[str]:
        """Canonical entity names mentioned in `text`, in order of first appearance."""
        if not self._terms:
            return []
        padded = f" {normalize_name(text)} "
        hits: list[tuple[int, str]] = []
        if self._ac is not None and _HAS_AC:
            for end, (term, canonical) in self._ac.iter(padded):
                start = end - len(term) + 1
                if padded[start - 1] == " " and padded[end + 1] == " ":
                    hits.append((start, canonical))
            hits.sort()
        else:
            rx = self._rx or self._build_regex()
            hits = [(m.start(), self._terms[m.group(1)]) for m in rx.finditer(padded)]
        found: list[str] = []
        for _, canonical in hits:
            if canonical not in found:
                found.append(canonical)
        return found

    def _build_regex(self) -> re.Pattern[str]:
        # Built lazily when the backend flag is flipped after construction (tests).
        alternation = "|".join(re.escape(t) for t in sorted(self._terms, key=len, reverse=True))
        self._rx = re.compile(rf"(?<!\S)({alternation})(?!\S)")
        return self._rx


class SpotterCache:
    """Per-user spotter, rebuilt when entities change or after `ttl_s` (other processes may write)."""

    def __init__(self, graph: _EntitySource, ttl_s: float = 300.0) -> None:
        self._graph = graph
        self._ttl = ttl_s
        self._cache: dict[int, tuple[float, EntitySpotter]] = {}

    async def get(self, user_id: int) -> EntitySpotter:
        hit = self._cache.get(user_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        spotter = EntitySpotter(await self._graph.entities(user_id))
        self._cache[user_id] = (time.monotonic() + self._ttl, spotter)
        return spotter

    def invalidate(self, user_id: int) -> None:
        self._cache.pop(user_id, None)
