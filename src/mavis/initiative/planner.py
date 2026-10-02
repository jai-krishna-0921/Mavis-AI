"""Deterministic plans used when the LLM is unavailable or silent."""

from __future__ import annotations

from datetime import timedelta

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, EventType
from mavis.domain.loops import Loop, LoopKind
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.filters import FilterResult
from mavis.timers.service import WakeupService

PREP_LEAD = timedelta(hours=1)
FOLLOW_UP_LAG = timedelta(hours=2)
PREP_MIN_IMPORTANCE = 4
FOLLOW_UP_MIN_IMPORTANCE = 3
SIGNAL_NOTIFY_RELEVANCE = 0.9


async def schedule_default_signals(wakeups: WakeupService, loop: Loop) -> list[int]:
    if loop.kind is not LoopKind.COMMITMENT or loop.due_at is None:
        return []
    now = timeutil.now()
    ids: list[int] = []
    if loop.importance >= PREP_MIN_IMPORTANCE and loop.due_at - PREP_LEAD > now:
        ids.append(await wakeups.wake_me(
            loop.user_id, loop.due_at - PREP_LEAD, f"Prep nudge before: {loop.title}", loop.id,
            WakeupKind.EVENT_STARTING, dedupe_key=f"loop:{loop.id}:starting"))
    if loop.importance >= FOLLOW_UP_MIN_IMPORTANCE and loop.due_at + FOLLOW_UP_LAG > now:
        ids.append(await wakeups.wake_me(loop.user_id, loop.due_at + FOLLOW_UP_LAG,
                                         f"Follow up on how it went: {loop.title}", loop.id,
                                         WakeupKind.EVENT_ENDED, dedupe_key=f"loop:{loop.id}:ended"))
    return ids


def fallback_decision(event: Event, result: FilterResult) -> InitiativeDecision:
    p = event.payload
    title = result.matched_loops[0].title if result.matched_loops else p.get("title") or p.get("reason", "")
    loop_id = p.get("loop_id")
    match event.type:
        case EventType.EVENT_STARTING:
            return InitiativeDecision(notify=NotifyIntent(
                urgency=3, intent=f"Short pep talk and prep reminder: '{title}' starts soon.",
                dedupe_key=f"prep:{loop_id}"))
        case EventType.EVENT_ENDED:
            return InitiativeDecision(notify=NotifyIntent(
                urgency=3, intent=f"Ask warmly how '{title}' went.", dedupe_key=f"followup:{loop_id}"))
        case EventType.USER_QUIET:
            return InitiativeDecision(notify=NotifyIntent(
                urgency=2, intent=f"Gentle, no-pressure nudge. No reply to: {p.get('question', '')}",
                dedupe_key=f"quiet:{p.get('wakeup_id')}"))
        case _ if result.relevance >= SIGNAL_NOTIFY_RELEVANCE and event.type is not EventType.LOOP_CREATED:
            return InitiativeDecision(notify=NotifyIntent(
                urgency=4, intent=f"Tell the user about this, briefly: {result.summary}",
                dedupe_key=f"signal:{event.id}"))
    return InitiativeDecision(ignore_reason="fallback: nothing to do")
