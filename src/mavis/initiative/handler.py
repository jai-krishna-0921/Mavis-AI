"""Entry point for every non-chat event (spec §4.3)."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from sqlalchemy.exc import NoResultFound

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import Loop, LoopKind, LoopOrigin, LoopStatus
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import hooks, subjects
from mavis.initiative.executor import (
    DEFERRED_TTL,
    REMINDER_PREFIX,
    REMINDER_URGENCY,
    InitiativeExecutor,
)
from mavis.initiative.filters import EXTERNAL_TYPES, EventFilter
from mavis.initiative.planner import (
    PREP_LEAD,
    PREP_MIN_IMPORTANCE,
    fallback_decision,
    schedule_default_signals,
)
from mavis.initiative.quiet import QuietTracker
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.routines import Routines
from mavis.initiative.untrusted import wrap_untrusted
from mavis.loops.service import LoopService
from mavis.policy.pings import normalize_dedupe_key
from mavis.store.repo import decisions as decisions_repo
from mavis.store.repo import users
from mavis.timers import system
from mavis.timers.service import WakeupService
from mavis.worker.runner import register_event_handler

log = structlog.get_logger()
MAX_WAKEUP_LATENESS = timedelta(hours=2)
MAX_LLM_URGENCY = 4
URGENT_URGENCY = 5
IMMINENT = PREP_LEAD + timedelta(minutes=10)  # the default prep wakeup (60 min ahead) plus slack
FOLLOW_UP_VALID_FOR = timedelta(hours=24)
LIVE_STATUSES = (LoopStatus.OPEN, LoopStatus.AWAITING_REPLY)
# WORKSPACE_SIGNAL belongs to the attention layer alone (amendment A1): third-party Drive/Docs/Tasks
# changes never reach the reasoner.
HANDLED_TYPES = tuple(
    t for t in EventType
    if t not in (EventType.USER_MESSAGE, EventType.BUTTON_PRESSED, EventType.WORKSPACE_SIGNAL)
)


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

        if event.type is EventType.WAKEUP and event.payload.get("reminder"):
            # A reminder the user asked for is a commitment, not a judgement call: no reasoner, no composer,
            # no daily budget, and never dropped for being late (it says so instead).
            reason = str(event.payload.get("reason", "")).removeprefix(REMINDER_PREFIX).strip()
            key = str(event.payload.get("reminder_key") or f"reminder:{event.payload.get('wakeup_id')}")
            if event.trust is Trust.UNTRUSTED:  # never set by wake_me; keep the cautious path just in case
                await self._executor.notify(
                    user, NotifyIntent(urgency=REMINDER_URGENCY, intent=f"Remind them: {reason}",
                                       dedupe_key=key), untrusted=True)
                return
            original_due = event.payload.get("original_due")
            due = datetime.fromisoformat(original_due) if original_due else event.occurred_at
            await self._executor.remind(user, reason, key, due)
            return
        if event.source == "timer" and _too_late(event):
            log.warning("initiative.wakeup_too_late", event_id=event.id, kind=kind,
                        due_at=event.occurred_at.isoformat())
            if kind == WakeupKind.ROUTINE.value:
                await self._routines.reschedule(user, event.payload.get("loop_id"))
            return
        if event.type is EventType.WAKEUP and kind == WakeupKind.DEFERRED.value:
            origin = event.payload.get("origin")
            if reason := await self._deferred_stale(user.id, event.payload):
                log.info("initiative.deferred_stale", event_id=event.id, origin=origin, reason=reason)
            else:
                original_due = event.payload.get("original_due")
                await self._executor.notify(
                    user, NotifyIntent.model_validate(event.payload["notify"]),
                    untrusted=bool(event.payload.get("untrusted", False)),
                    original_due=datetime.fromisoformat(original_due) if original_due else None,
                    origin=origin)
            return
        if event.type is EventType.WAKEUP and kind == WakeupKind.ROUTINE.value:
            await self._routines.run(user, event.payload)
            return
        if event.type is EventType.WAKEUP and kind == WakeupKind.AGENT.value and (
                why := await _agent_wakeup_stale(user.id, event)):
            # Revalidated when it fires: a closed or failed subject cancels its wakeups whatever the
            # subject's kind, and a wakeup bound to nothing (set before H5) never runs.
            log.info("initiative.agent_wakeup_dropped", event_id=event.id, reason=why)
            return
        if event.type is EventType.LOOP_UPDATED:
            await self._on_loop_updated(Loop.model_validate(event.payload))
            return
        if event.type is EventType.LOOP_CREATED and event.payload.get("kind") == LoopKind.ROUTINE.value:
            return  # routines schedule themselves
        if event.type is EventType.LOOP_CREATED and event.payload.get("origin") == LoopOrigin.REASONER.value:
            # The reasoner's own `track` is its belief, not news: feeding it back made echo pings about
            # the same thing under a new key. No reasoner run and no derived prep/follow-up signals.
            log.info("initiative.own_track_ignored", event_id=event.id)
            return
        if event.type is EventType.USER_QUIET:
            asked_at = datetime.fromisoformat(event.payload["asked_at"])
            if not await self._quiet.still_quiet(user.id, asked_at):
                return
            subject = subjects.Subject.parse(event.payload.get("subject"))
            if not await self._quiet.owed(user.id, subject):  # armed before H5, or the item moved on
                log.info("initiative.quiet_not_owed", event_id=event.id)
                return

        if event.type is EventType.EVENT_STARTING and not await self._origin_still_valid(
                user.id, {"kind": "event_starting", "loop_id": event.payload.get("loop_id")}):
            log.info("initiative.prep_dropped_started", event_id=event.id)
            return

        open_loops = await self._loops.active(user.id)
        result = await self._filter.apply(event, open_loops, user.timezone)
        if result.drop:
            log.info("initiative.dropped", event_id=event.id, reason=result.reason)
            return
        if not result.matched_loops and (reason := await hooks.run_prefilters(event)):
            log.info("initiative.prefiltered", event_id=event.id, reason=reason)
            return
        # A retry (the executor failed part way) re-applies the first decision: asking the model again
        # could decide differently after half of the first decision's side effects already happened.
        decision = await decisions_repo.get(event.id)
        if decision is None:
            decision = await self._decide(user, event, result)
            await decisions_repo.save(user.id, event.id, decision)
        else:
            log.info("initiative.decision_reused", event_id=event.id)

        if event.type is EventType.LOOP_CREATED:
            # Always: the model's own wakeups are deduped against these by the executor, not instead of them
            await schedule_default_signals(self._wakeups, Loop.model_validate(event.payload))
        context = result.summary
        if event.trust is Trust.UNTRUSTED:
            context = wrap_untrusted(result.summary, event.type.value)
        streak = int(event.payload.get("streak", 0)) + 1 if event.type is EventType.USER_QUIET else 0
        # Code evidence that a loop may have concluded: an external signal the filter matched to it by
        # its watch. Time passing, silence and the model's own belief are never evidence.
        external = event.type in EXTERNAL_TYPES
        evidence = frozenset(lp.id for lp in result.matched_loops) if external else frozenset()
        await self._executor.apply(user, decision, event, context=context, quiet_streak=streak,
                                  origin=await self._origin_for(event), evidence_loop_ids=evidence)
        # Silence never closes a loop: an EVENT_ENDED whose follow-up did not go out leaves it OPEN (it
        # stays visible, overdue, for the user to decide). Phase B's ledger keeps the same rule.

    async def _decide(self, user, event: Event, result) -> InitiativeDecision:
        result.extra = await hooks.gather_enrichments(event)
        try:
            decision = _normalize_llm_key(_cap_llm_urgency(await self._reasoner.decide(user, event, result)))
        except LLMError as exc:
            log.warning("initiative.reasoner_failed", event_id=event.id, error=str(exc))
            decision = fallback_decision(event, result)
        decision = await hooks.apply_decision_policies(event, decision)
        decision = _imminent_floor(event, decision, result.matched_loops)
        decision = _quiet_after_turn(event, decision)  # before persisting: a retry must not undo it
        return _with_default_dedupe(decision, event)

    async def _deferred_stale(self, user_id: int, payload: dict) -> str | None:
        """Send-time revalidation of a deferred ping: why it should no longer go out, or None."""
        origin = payload.get("origin") or {}
        valid_until = payload.get("valid_until") or origin.get("valid_until")
        if valid_until is None and payload.get("original_due"):  # deferred before valid_until existed
            valid_until = (datetime.fromisoformat(payload["original_due"]) + DEFERRED_TTL).isoformat()
        if valid_until and timeutil.now() > timeutil.ensure_utc(datetime.fromisoformat(valid_until)):
            return "expired"
        loop_id = payload.get("loop_id") or origin.get("loop_id")
        if loop_id is not None:
            loop = await self._loops.get(int(loop_id))
            if loop is None or loop.user_id != user_id or loop.status not in LIVE_STATUSES:
                return "loop closed"
        if origin and not await self._origin_still_valid(user_id, origin):
            return "origin stale"
        return None

    async def _origin_for(self, event: Event) -> dict | None:
        """Why a ping is being sent, carried with it if it is deferred so it can be revalidated."""
        if event.type is EventType.USER_QUIET:
            origin = {"kind": event.type.value, "asked_at": event.payload.get("asked_at")}
            if event.payload.get("subject"):  # the item the unanswered question is about
                origin["subject"] = event.payload["subject"]
            return origin
        loop_id = _event_loop_id(event)
        if loop_id is None:
            return None
        origin: dict = {"kind": event.type.value, "loop_id": loop_id}
        loop = await self._loops.get(loop_id)
        if loop is not None and loop.due_at is not None:
            due = timeutil.ensure_utc(loop.due_at)
            if event.type is EventType.EVENT_ENDED:
                origin["valid_until"] = (due + FOLLOW_UP_VALID_FOR).isoformat()
            elif due > timeutil.now():  # a reminder about something is stale once it has started
                origin["valid_until"] = due.isoformat()
        return origin

    async def _origin_still_valid(self, user_id: int, origin: dict) -> bool:
        """Send-time revalidation: has the reason for this message gone stale?"""
        kind = origin.get("kind")
        if kind == EventType.USER_QUIET.value and origin.get("asked_at"):
            return await self._quiet.still_quiet(user_id, datetime.fromisoformat(origin["asked_at"]))
        if kind == EventType.EVENT_STARTING.value and origin.get("loop_id"):
            loop = await self._loops.get(int(origin["loop_id"]))
            if loop is not None and loop.due_at is not None:
                return timeutil.ensure_utc(loop.due_at) > timeutil.now()
        return True

    async def _on_loop_updated(self, loop: Loop) -> None:
        if loop.status is not LoopStatus.OPEN:
            kinds = [k for k in WakeupKind if not k.value.startswith(system.SYSTEM_PREFIX)]
            if loop.status is LoopStatus.AWAITING_REPLY:  # the follow-up itself may still be deferred
                kinds = [k for k in kinds if k is not WakeupKind.DEFERRED]
            await self._wakeups.cancel_where(loop.user_id, kinds, loop_id=loop.id)
            return
        if loop.due_at is not None:  # due date may have moved: re-plan derived signals
            derived = [WakeupKind.EVENT_STARTING, WakeupKind.EVENT_ENDED]
            await self._wakeups.cancel_where(loop.user_id, derived, loop_id=loop.id)
            # an untrusted loop's re-planned signals fire as untrusted (capped at 4)
            await schedule_default_signals(self._wakeups, loop)


def _quiet_after_turn(event: Event, decision: InitiativeDecision) -> InitiativeDecision:
    """A loop extracted from a chat turn never triggers a ping or a task at creation: the user just
    talked about it and the chat turn owns any action (it can start_task or queue the approval
    itself; a second task from here duplicated work and approval cards). Tracking, default wakeups
    and the model's wakeups still apply; those do the follow-through later. (No time window: LEARN
    can run late under LLM load.)"""
    if event.type is not EventType.LOOP_CREATED or (decision.notify is None and not decision.act):
        return decision
    if event.payload.get("origin") != LoopOrigin.CONVERSATION.value:
        return decision
    intent = decision.notify.intent[:80] if decision.notify else ""
    log.info("initiative.post_turn_suppressed", event_id=event.id, intent=intent, acts=len(decision.act))
    return decision.model_copy(update={"notify": None, "act": [], "ignore_reason": "just discussed in chat"})


def _cap_llm_urgency(decision: InitiativeDecision) -> InitiativeDecision:
    """The model tends to call every pre-event nudge a 5. Only deterministic rules may produce 5
    (it bypasses quiet hours), so a model-proposed urgency is capped at 4."""
    if decision.notify is None:
        return decision
    notify = decision.notify.model_copy(update={"urgency": min(decision.notify.urgency, MAX_LLM_URGENCY),
                                                "security": False})  # only code may mark security
    return decision.model_copy(update={"notify": notify})


def _imminent_floor(event: Event, decision: InitiativeDecision, loops: list[Loop]) -> InitiativeDecision:
    """Deterministic rule: the prep nudge for a commitment starting within the prep lead is urgent."""
    if event.type is not EventType.EVENT_STARTING or decision.notify is None:
        return decision
    if event.trust is Trust.UNTRUSTED:  # a signal re-planned from third-party content never earns 5
        return decision
    loop_id = event.payload.get("loop_id")
    loop = next((lp for lp in loops if lp.id == loop_id), None)
    if loop is None or loop.due_at is None or loop.importance < PREP_MIN_IMPORTANCE:
        return decision
    if not timedelta(0) < timeutil.ensure_utc(loop.due_at) - timeutil.now() <= IMMINENT:
        return decision
    notify = decision.notify.model_copy(update={"urgency": URGENT_URGENCY})
    return decision.model_copy(update={"notify": notify})


async def _agent_wakeup_stale(user_id: int, event: Event) -> str | None:
    subject = subjects.event_subject(event)
    if subject is None:
        return "unbound"
    state = await subjects.resolve(user_id, subject)
    if state is None or not state.live:
        return f"subject {subject.key} closed"
    return None


def _too_late(event: Event) -> bool:
    return timeutil.now() - timeutil.ensure_utc(event.occurred_at) > MAX_WAKEUP_LATENESS


def _event_loop_id(event: Event) -> int | None:
    """The loop this event is about, if any."""
    raw = event.payload.get("loop_id")
    if raw is None and event.type in (EventType.LOOP_CREATED, EventType.LOOP_UPDATED):
        raw = event.payload.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_llm_key(decision: InitiativeDecision) -> InitiativeDecision:
    if decision.notify is None or not decision.notify.dedupe_key:
        return decision
    key = normalize_dedupe_key(decision.notify.dedupe_key)
    return decision.model_copy(update={"notify": decision.notify.model_copy(update={"dedupe_key": key})})


def _with_default_dedupe(decision: InitiativeDecision, event: Event) -> InitiativeDecision:
    if decision.notify is None or decision.notify.dedupe_key:
        return decision
    key = f"{event.type.value}:{event.payload.get('loop_id') or event.id}"
    return decision.model_copy(update={"notify": decision.notify.model_copy(update={"dedupe_key": key})})


def register(handler: InitiativeHandler) -> None:
    for event_type in HANDLED_TYPES:
        register_event_handler(event_type, handler.handle)
