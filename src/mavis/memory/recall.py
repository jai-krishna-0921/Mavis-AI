"""Budgeted assembly of the memory context block injected into prompts."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Protocol

from mavis.domain import timeutil
from mavis.domain.loops import Loop
from mavis.domain.memory import RecallContext
from mavis.domain.timefmt import due_label, message_stamp, stamped
from mavis.memory.extractor import wrap_untrusted
from mavis.memory.graph import Fact, GraphStore
from mavis.memory.spotter import SpotterCache
from mavis.memory.tokens import estimate_tokens
from mavis.memory.vector import VectorStore

log = logging.getLogger(__name__)

RECALL_TOKEN_BUDGET = 1200
RECALL_SOURCE_TIMEOUT_S = 0.5
DUE_SOON = timedelta(hours=48)


class LoopsReader(Protocol):
    async def active(
        self, user_id: int, entities: list[str] | None = None, due_within: timedelta | None = None
    ) -> list[Loop]: ...


def render_loop(loop: Loop, tz: str, now: datetime | None = None) -> str:
    """One loop for a prompt. The due time is relative to `now` (default: the clock at render time),
    computed here, so the model never does date arithmetic and never reads an overdue item as upcoming."""
    now = now or timeutil.now()
    parts = [loop.kind.value.lower().replace("_", " ")]
    if loop.due_at is not None:
        parts.append(due_label(loop.due_at, now, tz))
    if loop.created_at is not None:  # relative words in the title are relative to this (TIME_RULE)
        parts.append(f"created {message_stamp(loop.created_at, now, tz)}")
    line = f"{loop.title} ({', '.join(parts)})"
    # a loop derived from third-party content is data, never an instruction (spec 8.3)
    return line if loop.trusted else wrap_untrusted(line, source="loop")


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


def _render_loops(found: list[Loop], tz: str, tainted: set[str] | None = None) -> list[str]:
    out: list[str] = []
    now = timeutil.now()
    for loop in found:
        try:
            line = render_loop(loop, tz, now)
            out.append(line)
            if not loop.trusted and tainted is not None:
                tainted.add(line)
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
    tainted: set[str] = set()  # the items derived from third-party content, by identity

    async def _graph() -> list[str]:
        found = await graph.neighborhood(user_id, names) if names else []
        out = []
        for f in found:
            if isinstance(f, Fact) and f.third_party:
                # learned from a record (mail, Slack): labelled with where it came from, data not instruction
                f = Fact(wrap_untrusted(str(f), source=f.origin or "record"), f.source_ref)
                tainted.add(f)
            out.append(f)
        return out

    written: dict[str, datetime] = {}  # recalled text -> when it was written (for its stamp)

    async def _episodes() -> list[str]:
        hits = await vector.search_hits(user_id, text)
        # Third-party text (email, etc.) is stored as kind="signal"; it must never reach a prompt raw.
        out = []
        for t, kind, at in hits:
            if kind == "signal":
                t = wrap_untrusted(t, source="memory")
                tainted.add(t)
            if at is not None:
                written.setdefault(t, at)
            out.append(t)
        return out

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
        return _render_loops(merged, tz, tainted)

    facts, episodes, loop_lines = await asyncio.gather(
        _safe("graph", _graph(), []),
        _safe("vector", _episodes(), []),
        _safe("loops", _loops(), []),
    )
    ctx = assemble(profile, loop_lines, facts, episodes, budget)
    ctx.untrusted = any(item in tainted for item in [*ctx.loops, *ctx.facts, *ctx.episodes])
    # A recalled moment is replayed text: stamp it with when it was written (T1), after dedupe/budget.
    now = timeutil.now()
    ctx.episodes = [stamped(e, written[e], now, tz) if e in written else e for e in ctx.episodes]
    return ctx


async def _none() -> list[Loop]:
    return []
