"""MemoryService: the single entry point for recall (hot path) and learn (background)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from mavis.config import get_settings
from mavis.domain.events import Trust
from mavis.domain.memory import Extraction, RecallContext
from mavis.memory import recall as recall_mod
from mavis.memory.embeddings import Embedder, get_embedder
from mavis.memory.extractor import extract
from mavis.memory.graph import GraphStore, make_graph
from mavis.memory.recall import LoopsReader
from mavis.memory.resolver import resolve
from mavis.memory.spotter import SpotterCache
from mavis.memory.vector import QdrantVectorStore, VectorStore
from mavis.store.repo import profile as profile_repo
from mavis.store.repo import users

log = structlog.get_logger()

ExtractionHook = Callable[[int, Extraction, str], Awaitable[None]]
MIN_EPISODE_WORDS = 4
RECALL_TOTAL_TIMEOUT_S = 1.5
INIT_RETRY_COOLDOWN_S = 30.0
USER_PREFIX = "User: "


def user_message_of(text: str) -> str:
    """The user's own words in a conversation turn: text after the last line starting 'User: '."""
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(USER_PREFIX):
            return "\n".join([lines[i][len(USER_PREFIX):], *lines[i + 1:]]).strip()
    return text


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
        self._init_failed_at: float | None = None

    async def init(self) -> None:
        """Idempotent store initialisation; every public coroutine calls it lazily."""
        if self._ready:
            return
        async with self._init_lock:
            if not self._ready:
                await self.graph.init()
                await self.vector.init()
                self._ready = True

    async def warm(self) -> None:
        """Pay the one-off costs (store init, embedding model load) before the first reply."""
        await self.init()
        await self.embedder.embed(["warm-up"])

    def set_loops_reader(self, reader: LoopsReader | None) -> None:
        self.loops = reader

    def invalidate(self, user_id: int) -> None:
        self._spotters.invalidate(user_id)

    # --- hot path ------------------------------------------------------------------

    async def recall(self, user_id: int, text: str) -> RecallContext:
        """Never blocks a reply for long: the whole thing (incl. lazy init) is bounded."""
        try:
            return await asyncio.wait_for(self._recall(user_id, text), RECALL_TOTAL_TIMEOUT_S)
        except TimeoutError:
            if not self._ready:
                self._init_failed_at = time.monotonic()
            log.warning("memory.recall_timeout", timeout_s=RECALL_TOTAL_TIMEOUT_S)
            return RecallContext()

    async def _recall(self, user_id: int, text: str) -> RecallContext:
        if (
            not self._ready
            and self._init_failed_at is not None
            and time.monotonic() - self._init_failed_at < INIT_RETRY_COOLDOWN_S
        ):
            return RecallContext()
        try:
            try:
                await self.init()
            except Exception:
                self._init_failed_at = time.monotonic()
                raise
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
        trusted = trust is Trust.USER
        if trusted:
            # Third-party text must not seed graph entities: an email sender listed as an alias would
            # otherwise count as a "known sender" in the trusted triage signals.
            for entity in resolution.entities:
                await self.graph.upsert_entity(user_id, entity)
        facts = [r.statement for r in resolution.relations]
        if trusted:
            for rel in resolution.relations:
                await self.graph.upsert_relation(user_id, rel, source_ref=source_ref)
            await self.vector.add(user_id, facts, kind="fact", source_ref=source_ref)
            episode = user_message_of(text)
        else:
            # Third-party text: derived facts are signals (wrapped as untrusted in recall); no graph
            # relations, no profile or mood changes.
            await self.vector.add(user_id, facts, kind="signal", source_ref=source_ref)
            episode = text
        if len(episode.split()) >= MIN_EPISODE_WORDS:
            await self.vector.add(
                user_id, [episode[:500]], kind="episode" if trusted else "signal", source_ref=source_ref
            )

        if trusted and extraction.profile_updates:
            await profile_repo.save(user_id, card.apply(extraction.profile_updates))
        self.invalidate(user_id)

        final = extraction.model_copy(
            update={"entities": resolution.entities, "relations": resolution.relations}
        )
        if not trusted:
            final = final.model_copy(update={"mood": None})
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
