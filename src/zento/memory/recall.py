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
RECALL_SOURCE_TIMEOUT_S = 0.5
DUE_SOON = timedelta(hours=48)


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
        return await asyncio.wait_for(coro, RECALL_SOURCE_TIMEOUT_S)
    except TimeoutError:
        log.warning("recall: %s timed out after %.2fs; degrading to empty", what, RECALL_SOURCE_TIMEOUT_S)
    except Exception:
        log.warning("recall: %s failed; degrading to empty", what, exc_info=True)
    return default


def _render_loops(found: list[Loop], tz: str) -> list[str]:
    out: list[str] = []
    for loop in found:
        try:
            out.append(render_loop(loop, tz))
        except Exception:
            log.warning("recall: skipping unrenderable loop %s", getattr(loop, "id", "?"), exc_info=True)
    return out


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
    """LLM-free, parallel recall. Never raises or blocks: a failing or slow store yields an empty section."""

    async def _spot() -> list[str]:
        return (await spotters.get(user_id)).spot(text)

    names = await _safe("entity spotting", _spot(), [])

    async def _graph() -> list[str]:
        return await graph.neighborhood(user_id, names) if names else []

    async def _loops() -> list[str]:
        if loops is None:
            return []
        linked, soon = await asyncio.gather(
            _safe("linked loops", loops.active(user_id, entities=names), []) if names else _none(),
            _safe("due-soon loops", loops.active(user_id, entities=None, due_within=DUE_SOON), []),
        )
        soon = sorted(soon, key=lambda x: (x.due_at is None, x.due_at))
        seen: set[int] = set()
        merged: list[Loop] = []
        for loop in [*linked, *soon]:
            if loop.id not in seen:
                seen.add(loop.id)
                merged.append(loop)
        return _render_loops(merged, tz)

    facts, episodes, loop_lines = await asyncio.gather(
        _safe("graph", _graph(), []),
        _safe("vector", vector.search(user_id, text), []),
        _safe("loops", _loops(), []),
    )
    return assemble(profile, loop_lines, facts, episodes, budget)


async def _none() -> list[Loop]:
    return []
