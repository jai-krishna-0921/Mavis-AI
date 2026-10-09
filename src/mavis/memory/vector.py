"""Episodic / semantic memory in Qdrant.

Embedded (on-disk `path`) in dev, remote (`url`) in prod, `:memory:` in tests.
Point ids are deterministic so re-learning the same sentence is idempotent.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

import structlog
from qdrant_client import AsyncQdrantClient, models

from mavis.domain import timeutil
from mavis.memory.embeddings import Embedder

log = structlog.get_logger()
COLLECTION = "episodes"
# the fence around the assistant's previous reply in LEARN text (mavis.memory.service.CONTEXT_OPEN/CLOSE)
ASSISTANT_FENCE = ("<assistant_context>", "</assistant_context>")
_OPEN, _CLOSE = (re.escape(m) for m in ASSISTANT_FENCE)
_FENCED = re.compile(_OPEN + r".*?(?:" + _CLOSE + r"|$)", re.DOTALL)


def strip_assistant_fence(text: str) -> str:
    """`text` without any fenced assistant block (an unclosed one runs to the end); the rest, which is
    the user's own words, is kept."""
    return _FENCED.sub(" ", text).replace(ASSISTANT_FENCE[1], " ")


def _ts(raw: object) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class VectorStore(Protocol):
    async def init(self) -> None: ...
    async def add(
        self, user_id: int, texts: list[str], kind: str, source_ref: str = "", at: datetime | None = None
    ) -> None: ...
    async def search(self, user_id: int, query: str, k: int = 6, min_score: float = 0.35) -> list[str]: ...
    async def search_with_kind(
        self, user_id: int, query: str, k: int = 6, min_score: float = 0.35
    ) -> list[tuple[str, str]]: ...
    async def search_hits(
        self, user_id: int, query: str, k: int = 6, min_score: float = 0.35
    ) -> list[tuple[str, str, datetime | None]]: ...
    async def forget(self, user_id: int, needle: str) -> int: ...
    async def forget_source(self, user_id: int, prefix: str) -> int: ...
    async def forget_where(self, user_id: int, match: Callable[[str, str, str], bool]) -> int: ...
    async def count(self, user_id: int) -> int: ...
    async def delete_user(self, user_id: int) -> int: ...


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
            await self._ensure_tenant_index()
            return
        dim = await asyncio.to_thread(lambda: self._embedder.dim)
        await self._client.create_collection(
            COLLECTION, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
        )
        await self._ensure_tenant_index()

    async def _ensure_tenant_index(self) -> None:
        """user_id is the tenant key: Qdrant co-locates one user's vectors (spec 6.1). A no-op in embedded
        mode; if the server cannot change an existing index the old one stays and the filter isolates."""
        if not self._remote:  # payload indexes are a no-op (with a warning) in embedded mode
            return
        try:
            await self._client.create_payload_index(
                COLLECTION, "user_id",
                models.IntegerIndexParams(type=models.IntegerIndexType.INTEGER, is_tenant=True, lookup=True,
                                          range=False))
        except Exception as exc:  # noqa: BLE001
            log.warning("vector.tenant_index_unsupported", error=type(exc).__name__)

    async def add(
        self, user_id: int, texts: list[str], kind: str, source_ref: str = "", at: datetime | None = None
    ) -> None:
        """`at`: when the text was written (a turn's time), default now. Recall stamps hits with it."""
        unique: dict[str, str] = {}
        for t in texts:
            if any(marker in t for marker in ASSISTANT_FENCE):
                log.error("memory.assistant_text_stripped", user_id=user_id, kind=kind, source_ref=source_ref)
                t = strip_assistant_fence(t)  # the assistant's words are never a memory source; the rest is
            if t and t.strip():
                unique.setdefault(self.point_id(user_id, t), " ".join(t.split()))
        if not unique:
            return
        clean = list(unique.values())
        vectors = await self._embedder.embed(clean)
        now = (at.astimezone(UTC) if at and at.tzinfo else at.replace(tzinfo=UTC) if at
               else timeutil.now()).isoformat()
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
        return [(text, kind) for text, kind, _ in await self.search_hits(user_id, query, k, min_score)]

    async def search_hits(
        self, user_id: int, query: str, k: int = 6, min_score: float = 0.35
    ) -> list[tuple[str, str, datetime | None]]:
        """(text, kind, written_at): written_at is None for points stored without a time."""
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
        return [(p.payload["text"], str(p.payload.get("kind", "episode")), _ts(p.payload.get("ts")))
                for p in res.points if p.payload]

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

    async def delete_user(self, user_id: int) -> int:
        """Remove every point of one user (account deletion). Returns how many there were."""
        n = await self.count(user_id)
        if n:
            await self._client.delete(COLLECTION, points_selector=models.FilterSelector(
                filter=_user_filter(user_id)))
        return n

    async def forget_where(self, user_id: int, match: Callable[[str, str, str], bool]) -> int:
        """Delete this user's points for which `match(text, source_ref, kind)` holds (the caller decides
        what a point being about something means; no substring semantics here)."""
        ids = [p.id for p in await self._scroll_user(user_id)
               if match(str((p.payload or {}).get("text", "")), str((p.payload or {}).get("source_ref", "")),
                        str((p.payload or {}).get("kind", "")))]
        if ids:
            await self._client.delete(COLLECTION, points_selector=models.PointIdsList(points=ids))
        return len(ids)

    async def forget_source(self, user_id: int, prefix: str) -> int:
        """Delete the points learned from records whose source_ref starts with `prefix`."""
        if not prefix.strip():
            return 0
        ids = [p.id for p in await self._scroll_user(user_id)
               if str((p.payload or {}).get("source_ref", "")).startswith(prefix)]
        if ids:
            await self._client.delete(COLLECTION, points_selector=models.PointIdsList(points=ids))
        return len(ids)

    async def count(self, user_id: int) -> int:
        res = await self._client.count(COLLECTION, count_filter=_user_filter(user_id), exact=True)
        return res.count

    async def close(self) -> None:
        await self._client.close()
