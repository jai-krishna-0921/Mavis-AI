"""Episodic / semantic memory in Qdrant.

Embedded (on-disk `path`) in dev, remote (`url`) in prod, `:memory:` in tests.
Point ids are deterministic so re-learning the same sentence is idempotent.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Protocol

import structlog
from qdrant_client import AsyncQdrantClient, models

from mavis.memory.embeddings import Embedder

log = structlog.get_logger()
COLLECTION = "episodes"


class VectorStore(Protocol):
    async def init(self) -> None: ...
    async def add(self, user_id: int, texts: list[str], kind: str, source_ref: str = "") -> None: ...
    async def search(self, user_id: int, query: str, k: int = 6, min_score: float = 0.35) -> list[str]: ...
    async def search_with_kind(
        self, user_id: int, query: str, k: int = 6, min_score: float = 0.35
    ) -> list[tuple[str, str]]: ...
    async def forget(self, user_id: int, needle: str) -> int: ...
    async def count(self, user_id: int) -> int: ...


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _user_filter(user_id: int) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))])


class QdrantVectorStore:
    def __init__(
        self,
        embedder: Embedder,
        *,
        url: str | None = None,
        path: str | None = None,
        location: str | None = None,
    ) -> None:
        self._embedder = embedder
        self._remote = bool(url)
        if url:
            self._client = AsyncQdrantClient(url=url)
        elif location:
            self._client = AsyncQdrantClient(location=location)
        else:
            self._client = AsyncQdrantClient(path=path)

    @property
    def client(self) -> AsyncQdrantClient:
        """The underlying client, shared with the attention index (embedded Qdrant locks its directory)."""
        return self._client

    @staticmethod
    def point_id(user_id: int, text: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mavis:{user_id}:{_norm(text)}"))

    async def init(self) -> None:
        if await self._client.collection_exists(COLLECTION):
            return
        dim = await asyncio.to_thread(lambda: self._embedder.dim)
        await self._client.create_collection(
            COLLECTION, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
        )
        if self._remote:  # payload indexes are a no-op (with a warning) in embedded mode
            await self._client.create_payload_index(COLLECTION, "user_id", models.PayloadSchemaType.INTEGER)

    async def add(self, user_id: int, texts: list[str], kind: str, source_ref: str = "") -> None:
        unique: dict[str, str] = {}
        for t in texts:
            if t and t.strip():
                unique.setdefault(self.point_id(user_id, t), " ".join(t.split()))
        if not unique:
            return
        clean = list(unique.values())
        vectors = await self._embedder.embed(clean)
        now = datetime.now(UTC).isoformat()
        await self._client.upsert(
            COLLECTION,
            points=[
                models.PointStruct(
                    id=pid,
                    vector=vec,
                    payload={
                        "user_id": user_id,
                        "text": text,
                        "kind": kind,
                        "source_ref": source_ref,
                        "ts": now,
                    },
                )
                for (pid, text), vec in zip(unique.items(), vectors, strict=True)
            ],
        )

    async def search(self, user_id: int, query: str, k: int = 6, min_score: float = 0.35) -> list[str]:
        return [text for text, _ in await self.search_with_kind(user_id, query, k, min_score)]

    async def search_with_kind(
        self, user_id: int, query: str, k: int = 6, min_score: float = 0.35
    ) -> list[tuple[str, str]]:
        """Like search, but each hit is (text, kind) so callers can treat third-party signals as untrusted."""
        if not query.strip():
            return []
        [vec] = await self._embedder.embed([query])
        res = await self._client.query_points(
            COLLECTION,
            query=vec,
            limit=k,
            score_threshold=min_score,
            query_filter=_user_filter(user_id),
            with_payload=True,
        )
        return [(p.payload["text"], str(p.payload.get("kind", "episode"))) for p in res.points if p.payload]

    async def _scroll_user(self, user_id: int) -> list[models.Record]:
        out: list[models.Record] = []
        offset = None
        while True:
            points, offset = await self._client.scroll(
                COLLECTION,
                scroll_filter=_user_filter(user_id),
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            out.extend(points)
            if offset is None:
                return out

    async def forget(self, user_id: int, needle: str) -> int:
        n = needle.casefold().strip()
        if not n:
            return 0
        ids = [
            p.id
            for p in await self._scroll_user(user_id)
            if n in str((p.payload or {}).get("text", "")).casefold()
        ]
        if ids:
            await self._client.delete(COLLECTION, points_selector=models.PointIdsList(points=ids))
        return len(ids)

    async def count(self, user_id: int) -> int:
        res = await self._client.count(COLLECTION, count_filter=_user_filter(user_id), exact=True)
        return res.count

    async def close(self) -> None:
        await self._client.close()
