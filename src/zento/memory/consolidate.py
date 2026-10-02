"""Nightly consolidation: rewrite the profile card from recent facts, merge duplicate entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field

from zento.domain.errors import LLMError
from zento.llm import models as llm
from zento.memory.embeddings import Embedder, cosine
from zento.memory.graph import GraphStore
from zento.memory.names import normalize_name
from zento.memory.profile import LIST_FIELDS, MAX_ITEMS
from zento.store.repo import profile as profile_repo

if TYPE_CHECKING:
    from zento.memory.service import MemoryService

log = structlog.get_logger()
DUP_SIM = 0.92
MAX_FACTS = 60

_SYSTEM = (
    "You maintain a compact profile card about a user for their personal assistant. "
    "Rewrite the card from the current card plus the facts below. Keep only stable, useful traits. "
    "Each list at most 8 short items, "
    "most important first. Do not invent anything the card or facts do not support."
)


class ProfileDraft(BaseModel):
    name: str | None = None
    tone: str | None = None
    goals: list[str] = Field(default_factory=list)
    key_people: list[str] = Field(default_factory=list)
    routines: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)


async def merge_duplicates(user_id: int, graph: GraphStore, embedder: Embedder) -> int:
    entities = await graph.entities(user_id)
    by_label: dict[str, list] = {}
    for e in entities:
        by_label.setdefault(e.label, []).append(e)
    merged = 0
    for label, group in by_label.items():
        if len(group) < 2:
            continue
        vectors = await embedder.embed([e.name for e in group])
        dropped: set[int] = set()
        for i, keep in enumerate(group):
            if i in dropped:
                continue
            keep_terms = {normalize_name(keep.name), *(normalize_name(a) for a in keep.aliases)}
            for j in range(i + 1, len(group)):
                if j in dropped:
                    continue
                other = group[j]
                same_alias = normalize_name(other.name) in keep_terms
                if same_alias or cosine(vectors[i], vectors[j]) >= DUP_SIM:
                    await graph.merge_entities(user_id, keep=keep.name, drop=other.name, label=label)
                    dropped.add(j)
                    merged += 1
    return merged


async def consolidate(user_id: int, memory: MemoryService) -> dict:
    merged = await merge_duplicates(user_id, memory.graph, memory.embedder)
    facts = [d["statement"] for d in await memory.graph.dump(user_id)][-MAX_FACTS:]
    rewritten = False
    if facts:
        card = await profile_repo.get(user_id)
        fact_lines = "\n".join(f"- {f}" for f in facts)
        prompt = f"Current card:\n{card.render() or '(empty)'}\n\nFacts:\n{fact_lines}"
        try:
            draft = await llm.structured(ProfileDraft, _SYSTEM, prompt, llm.Tier.SMART)
        except LLMError as exc:
            log.warning("memory.consolidate_failed", user_id=user_id, error=str(exc))
        else:
            update = {f: [x for x in getattr(draft, f) if x.strip()][:MAX_ITEMS] for f in LIST_FIELDS}
            update["name"] = draft.name or card.name
            update["tone"] = draft.tone or card.tone
            await profile_repo.save(user_id, card.model_copy(update=update))
            rewritten = True
    memory.invalidate(user_id)
    return {"merged": merged, "profile_rewritten": rewritten}
