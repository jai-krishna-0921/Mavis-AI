"""Entry point for every non-chat event (spec §4.3)."""

from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy.exc import NoResultFound

from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import Loop, LoopKind, LoopStatus
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.executor import InitiativeExecutor
from mavis.initiative.filters import EventFilter
from mavis.initiative.planner import fallback_decision, schedule_default_signals
from mavis.initiative.quiet import QuietTracker
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.routines import Routines
from mavis.initiative.untrusted import wrap_untrusted
from mavis.loops.service import LoopService
from mavis.store.repo import users
from mavis.timers import system
from mavis.timers.service import WakeupService
from mavis.worker.runner import register_event_handler

log = structlog.get_logger()
HANDLED_TYPES = tuple(t for t in EventType if t not in (EventType.USER_MESSAGE, EventType.BUTTON_PRESSED))


class InitiativeHandler:
    def __init__(self, *, filt: EventFilter, reasoner: Reasoner, executor: InitiativeExecutor,
                 loops: LoopService, wakeups: WakeupService, routines: Routines, quiet: QuietTracker) -> None:
        self._filter, self._reasoner, self._executor = filt, reasoner, executor
        self._loops, self._wakeups, self._routines, self._quiet = loops, wakeups, routines, quiet

    async def handle(self, event: Event) -> None:
        if event.type is EventType.WAKEUP and await system.dispatch_system_wakeup(event):
            return  # Phase 4/5 plumbing (approval reminders, polls, connection checks)
        try:
            user = await users.get(event.user_id)
        except NoResultFound:
            log.warning("initiative.user_missing", event_id=event.id, user_id=event.user_id)
            return
        kind = event.payload.get("kind")

        if event.type is EventType.WAKEUP and kind == WakeupKind.DEFERRED.value:
            await self._executor.notify(user, NotifyIntent.model_validate(event.payload["notify"]),
                                        untrusted=bool(event.payload.get("untrusted", False)))
            return
        if event.type is EventType.WAKEUP and kind == WakeupKind.ROUTINE.value:
            await self._routines.run(user, event.payload)
            return
        if event.type is EventType.LOOP_UPDATED:
            await self._on_loop_updated(Loop.model_validate(event.payload))
            return
        if event.type is EventType.LOOP_CREATED and event.payload.get("kind") == LoopKind.ROUTINE.value:
            return  # routines schedule themselves
        if event.type is EventType.USER_QUIET:
            asked_at = datetime.fromisoformat(event.payload["asked_at"])
            if not await self._quiet.still_quiet(user.id, asked_at):
                return

        open_loops = await self._loops.active(user.id)
        result = await self._filter.apply(event, open_loops)
        if result.drop:
            log.info("initiative.dropped", event_id=event.id, reason=result.reason)
            return
        try:
            decision = await self._reasoner.decide(user, event, result)
        except LLMError as exc:
            log.warning("initiative.reasoner_failed", event_id=event.id, error=str(exc))
            decision = fallback_decision(event, result)

        if event.type is EventType.LOOP_CREATED:
            loop = Loop.model_validate(event.payload)
            if not any(w.loop_id == loop.id for w in decision.wakeups):
                await schedule_default_signals(self._wakeups, loop)

        decision = _with_default_dedupe(decision, event)
        context = result.summary
        if event.trust is Trust.UNTRUSTED:
            context = wrap_untrusted(result.summary, event.type.value)
        streak = int(event.payload.get("streak", 0)) + 1 if event.type is EventType.USER_QUIET else 0
        await self._executor.apply(user, decision, event, context=context, quiet_streak=streak)

        if event.type is EventType.EVENT_ENDED and event.payload.get("loop_id"):
            await self._loops.close(int(event.payload["loop_id"]), LoopStatus.DONE)

    async def _on_loop_updated(self, loop: Loop) -> None:
        if loop.status is not LoopStatus.OPEN:
            kinds = [k for k in WakeupKind if not k.value.startswith(system.SYSTEM_PREFIX)]
            await self._wakeups.cancel_where(loop.user_id, kinds, loop_id=loop.id)
            return
        if loop.due_at is not None:  # due date may have moved: re-plan derived signals
            derived = [WakeupKind.EVENT_STARTING, WakeupKind.EVENT_ENDED]
            await self._wakeups.cancel_where(loop.user_id, derived, loop_id=loop.id)
            await schedule_default_signals(self._wakeups, loop)


def _with_default_dedupe(decision: InitiativeDecision, event: Event) -> InitiativeDecision:
    if decision.notify is None or decision.notify.dedupe_key:
        return decision
    key = f"{event.type.value}:{event.payload.get('loop_id') or event.id}"
    return decision.model_copy(update={"notify": decision.notify.model_copy(update={"dedupe_key": key})})


def register(handler: InitiativeHandler) -> None:
    for event_type in HANDLED_TYPES:
        register_event_handler(event_type, handler.handle)
