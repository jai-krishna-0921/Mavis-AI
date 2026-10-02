"""Budgeted assembly of the memory context block injected into prompts."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from zento.domain.loops import Loop
from zento.domain.memory import RecallContext
from zento.memory.graph import GraphStore
from zento.memory.spotter import SpotterCache
from zento.memory.tokens import estimate_tokens
from zento.memory.vector import VectorStore

log = logging.getLogger(__name__)

RECALL_TOKEN_BUDGET = 1200


class LoopsReader(Protocol):
    async def active(
        self, user_id: int, entities: list[str] | None = None, due_within: timedelta | None = None
    ) -> list[Loop]: ...


def render_loop(loop: Loop, tz: str) -> str:
    kind = loop.kind.value.lower().replace("_", " ")
    if loop.due_at is None:
        return f"{loop.title} ({kind})"
    local = loop.due_at.astimezone(ZoneInfo(tz))
    return f"{loop.title} ({kind}, due {local.strftime('%a %d %b %H:%M')})"


def assemble(
    profile: str, loops: list[str], facts: list[str], episodes: list[str], budget: int = RECALL_TOKEN_BUDGET
) -> RecallContext:
    """Fill the budget in priority order: profile, loops, facts, episodes (episodes dedupe against facts)."""
    used = estimate_tokens(profile)
    if used > budget:
        profile = profile[: budget * 4]
        used = budget

    def take(items: list[str], skip: set[str]) -> list[str]:
        nonlocal used
        out: list[str] = []
        for item in items:
            if item in skip or item in out:
                continue
            cost = estimate_tokens(item) + 1
            if used + cost > budget:
                break
            out.append(item)
            used += cost
        return out

    kept_loops = take(loops, set())
    kept_facts = take(facts, set())
    kept_episodes = take(episodes, set(kept_facts))
    return RecallContext(profile=profile, loops=kept_loops, facts=kept_facts, episodes=kept_episodes)


async def _safe[T](what: str, coro, default: T) -> T:
    try:
        return await coro
    except Exception:
        log.warning("recall: %s failed; degrading to empty", what, exc_info=True)
        return default


async def recall(
    user_id: int,
    text: str,
    *,
    profile: str,
    tz: str,
    spotters: SpotterCache,
    graph: GraphStore,
    vector: VectorStore,
    loops: LoopsReader | None = None,
    budget: int = RECALL_TOKEN_BUDGET,
) -> RecallContext:
    """LLM-free, parallel recall. Never raises: a failing store yields an empty section."""
    try:
        spotter = await spotters.get(user_id)
        names = spotter.spot(text)
    except Exception:
        log.warning("recall: entity spotting failed", exc_info=True)
        names = []

    async def _graph() -> list[str]:
        return await graph.neighborhood(user_id, names) if names else []

    async def _loops() -> list[str]:
        if loops is None:
            return []
        found = await loops.active(user_id, names or None)
        return [render_loop(x, tz) for x in found]

    facts, episodes, loop_lines = await asyncio.gather(
        _safe("graph", _graph(), []),
        _safe("vector", vector.search(user_id, text), []),
        _safe("loops", _loops(), []),
    )
    return assemble(profile, loop_lines, facts, episodes, budget)
