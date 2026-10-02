"""Text embeddings behind a small port so tests never download a model.

Production uses fastembed (ONNX on CPU, ~5 ms/sentence, no API key). The model
loads lazily on first use, inside a worker thread, so importing this module and
constructing the embedder are free.
"""

from __future__ import annotations

import asyncio
import math
from functools import lru_cache
from typing import Any, Protocol

from mavis.config import get_settings


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: str) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: Any = None
        self._dim: int | None = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self._model_name, cache_dir=self._cache_dir)
        return self._model

    @property
    def dim(self) -> int:  # blocking on first call; callers wrap in to_thread
        if self._dim is None:
            self._dim = len(next(iter(self._load().embed(["probe"]))))
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(lambda: [v.tolist() for v in self._load().embed(texts)])


_override: Embedder | None = None


def set_embedder(embedder: Embedder | None) -> None:
    global _override
    _override = embedder


@lru_cache
def _default() -> FastEmbedder:
    s = get_settings()
    return FastEmbedder(s.embedding_model, str(s.data_dir / "models"))


def get_embedder() -> Embedder:
    return _override or _default()


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)
