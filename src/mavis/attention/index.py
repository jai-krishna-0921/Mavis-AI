"""Embeddings for the attention layer: observation search for chat, preference kNN and novelty.

Reuses the process embedder (one fastembed model in RAM) and the memory service's Qdrant client, in its
own collection so third-party summaries never surface as memory episodes. Only sanitized summaries
(kind, domain label, short scrubbed subject) are embedded; bodies never are."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models

from mavis.memory.embeddings import Embedder

COLLECTION = "attention"
OBS, PREF = "obs", "pref"


@dataclass(frozen=True)
class PrefHit:
    sentiment: str
    kind: str
    score: float


def point_id(user_id: int, typ: str, ref: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mavis:attention:{user_id}:{typ}:{ref}"))


def _filter(user_id: int, typ: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
            models.FieldCondition(key="type", match=models.MatchValue(value=typ)),
        ]
    )


class AttentionIndex:
    def __init__(self, client: AsyncQdrantClient, embedder: Embedder, *, remote: bool = False) -> None:
        self._client, self._embedder, self._remote = client, embedder, remote
        self._ready = False
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            if not await self._client.collection_exists(COLLECTION):
                dim = await asyncio.to_thread(lambda: self._embedder.dim)
                await self._client.create_collection(
                    COLLECTION, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
                )
                if self._remote:  # payload indexes are a no-op (with a warning) in embedded mode
                    await self._client.create_payload_index(
                        COLLECTION, "user_id", models.PayloadSchemaType.INTEGER
                    )
                    await self._client.create_payload_index(
                        COLLECTION, "type", models.PayloadSchemaType.KEYWORD
                    )
            self._ready = True

    async def embed(self, text: str) -> list[float]:
        [vector] = await self._embedder.embed([text])
        return vector

    async def _upsert(self, pid: str, vector: list[float], payload: dict) -> str:
        await self.init()
        await self._client.upsert(
            COLLECTION, points=[models.PointStruct(id=pid, vector=vector, payload=payload)]
        )
        return pid

    async def add_observation(
        self, user_id: int, obs_id: int, kind: str, vector: list[float], ts_iso: str
    ) -> str:
        return await self._upsert(
            point_id(user_id, OBS, obs_id),
            vector,
            {"user_id": user_id, "type": OBS, "ref": obs_id, "kind": kind, "ts": ts_iso},
        )

    async def add_pref(
        self, user_id: int, pref_id: int, sentiment: str, kind: str, vector: list[float]
    ) -> str:
        return await self._upsert(
            point_id(user_id, PREF, pref_id),
            vector,
            {"user_id": user_id, "type": PREF, "ref": pref_id, "kind": kind, "sentiment": sentiment},
        )

    async def novelty(self, user_id: int, vector: list[float]) -> float:
        """1 - max cosine to this user's observations; 1.0 when there are none."""
        await self.init()
        res = await self._client.query_points(
            COLLECTION, query=vector, limit=1, query_filter=_filter(user_id, OBS), with_payload=False
        )
        if not res.points:
            return 1.0
        return round(max(0.0, min(1.0, 1.0 - res.points[0].score)), 3)

    async def prefs_near(
        self, user_id: int, vector: list[float], min_score: float, k: int = 5
    ) -> list[PrefHit]:
        await self.init()
        res = await self._client.query_points(
            COLLECTION,
            query=vector,
            limit=k,
            score_threshold=min_score,
            query_filter=_filter(user_id, PREF),
            with_payload=True,
        )
        return [
            PrefHit(str(p.payload.get("sentiment", "")), str(p.payload.get("kind", "")), float(p.score))
            for p in res.points
            if p.payload
        ]

    async def search(self, user_id: int, query: str, k: int = 3, min_score: float = 0.5) -> list[int]:
        if not query.strip():
            return []
        await self.init()
        res = await self._client.query_points(
            COLLECTION,
            query=await self.embed(query),
            limit=k,
            score_threshold=min_score,
            query_filter=_filter(user_id, OBS),
            with_payload=True,
        )
        return [int(p.payload["ref"]) for p in res.points if p.payload]

    async def delete(self, point_ids: list[str]) -> None:
        if not point_ids:
            return
        await self.init()
        await self._client.delete(COLLECTION, points_selector=models.PointIdsList(points=point_ids))
