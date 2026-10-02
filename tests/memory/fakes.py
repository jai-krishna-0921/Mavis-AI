"""Deterministic embedders for tests: no model download, no network."""

from __future__ import annotations

import hashlib
import math
import re


class HashEmbedder:
    """Bag-of-hashed-words embedding. Texts sharing words get high cosine."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


class TableEmbedder:
    """Returns exactly the vectors a test specifies; unknown text is a test bug."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self._table = table
        self.dim = len(next(iter(table.values())))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._table[t] for t in texts]
