"""MemoryService: the single entry point for recall (hot path) and learn (background)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from zento.config import get_settings
from zento.domain.events import Trust
from zento.domain.memory import Extraction, RecallContext
from zento.memory import recall as recall_mod
from zento.memory.embeddings import Embedder, get_embedder
from zento.memory.extractor import extract
from zento.memory.graph import GraphStore, make_graph
from zento.memory.recall import LoopsReader
from zento.memory.resolver import resolve
from zento.memory.spotter import SpotterCache
from zento.memory.vector import QdrantVectorStore, VectorStore
from zento.store.repo import profile as profile_repo
from zento.store.repo import users

log = structlog.get_logger()

ExtractionHook = Callable[[int, Extraction, str], Awaitable[None]]
MIN_EPISODE_WORDS = 4


class MemoryService:
    def __init__(
        self, graph: GraphStore, vector: VectorStore, embedder: Embedder, loops: LoopsReader | None = None
    ) -> None:
        self.graph = graph
        self.vector = vector
        self.embedder = embedder
        self.loops = loops
        self.on_extraction: list[ExtractionHook] = []
        self._spotters = SpotterCache(graph)
        self._ready = False
        self._init_lock = asyncio.Lock()

    async def init(self) -> None:
        """Idempotent store initialisation; every public coroutine calls it lazily."""
        if self._ready:
            return
        async with self._init_lock:
            if not self._ready:
                await self.graph.init()
                await self.vector.init()
                self._ready = True

    def set_loops_reader(self, reader: LoopsReader | None) -> None:
        self.loops = reader

    def invalidate(self, user_id: int) -> None:
        self._spotters.invalidate(user_id)

    # --- hot path ------------------------------------------------------------------

    async def recall(self, user_id: int, text: str) -> RecallContext:
        try:
            await self.init()
            user = await users.get(user_id)
            card = await profile_repo.get(user_id)
            profile, tz = card.render(), user.timezone
        except Exception:
            log.warning("memory.recall_setup_failed", exc_info=True)
            return RecallContext()
        return await recall_mod.recall(
            user_id,
            text,
            profile=profile,
            tz=tz,
            spotters=self._spotters,
            graph=self.graph,
            vector=self.vector,
            loops=self.loops,
        )

    # --- background ----------------------------------------------------------------

    async def learn(
        self, user_id: int, text: str, source_ref: str = "", trust: Trust = Trust.USER
    ) -> Extraction:
        """Extract and persist. LLMError from extraction propagates so the LEARN job retries.

        Idempotent on retry: graph writes are MERGEs, vector ids are uuid5 of the text.
        """
        await self.init()
        user = await users.get(user_id)
        card = await profile_repo.get(user_id)
        extraction = await extract(
            text, user_name=user.name or card.name, tz=user.timezone, trust=trust, source=source_ref
        )
        if not card.tracks_mood:
            extraction = extraction.model_copy(update={"mood": None})

        resolution = await resolve(extraction, await self.graph.entities(user_id), self.embedder)
        for entity in resolution.entities:
            await self.graph.upsert_entity(user_id, entity)
        for rel in resolution.relations:
            await self.graph.upsert_relation(user_id, rel, source_ref=source_ref)

        facts = [r.statement for r in resolution.relations]
        await self.vector.add(user_id, facts, kind="fact", source_ref=source_ref)
        if len(text.split()) >= MIN_EPISODE_WORDS:
            kind = "episode" if trust is Trust.USER else "signal"
            await self.vector.add(user_id, [text[:500]], kind=kind, source_ref=source_ref)

        if extraction.profile_updates:
            await profile_repo.save(user_id, card.apply(extraction.profile_updates))
        self.invalidate(user_id)

        final = extraction.model_copy(
            update={"entities": resolution.entities, "relations": resolution.relations}
        )
        for hook in self.on_extraction:
            try:
                await hook(user_id, final, source_ref)
            except Exception:
                log.error("memory.hook_failed", hook=repr(hook), exc_info=True)
        return final

    # --- user control ------------------------------------------------------------

    async def describe_user(self, user_id: int) -> str:
        await self.init()
        card = await profile_repo.get(user_id)
        facts = [d["statement"] for d in await self.graph.dump(user_id)][-15:]
        parts = []
        if rendered := card.render():
            parts.append(rendered)
        if facts:
            parts.append("Things I've picked up:\n" + "\n".join(f"- {f}" for f in facts))
        return "\n\n".join(parts) or "I don't know much about you yet."

    async def forget(self, user_id: int, needle: str) -> int:
        if not needle.strip():
            return 0
        await self.init()
        removed = await self.graph.forget(user_id, needle) + await self.vector.forget(user_id, needle)
        card, changed = (await profile_repo.get(user_id)).remove_matching(needle)
        if changed:
            await profile_repo.save(user_id, card)
            removed += 1
        self.invalidate(user_id)
        return removed


_service: MemoryService | None = None


def set_memory(svc: MemoryService | None) -> None:
    global _service
    _service = svc


def get_memory() -> MemoryService:
    """Synchronous lazy singleton. Stores initialise on first use (see MemoryService.init)."""
    global _service
    if _service is None:
        s = get_settings()
        embedder = get_embedder()
        if s.qdrant_url:
            vector = QdrantVectorStore(embedder, url=s.qdrant_url)
        else:  # embedded: path=None would silently target localhost:6333
            vector = QdrantVectorStore(embedder, path=str(s.data_dir / "qdrant"))
        _service = MemoryService(make_graph(), vector, embedder)
    return _service
