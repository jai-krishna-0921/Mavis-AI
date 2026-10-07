"""Apply an InitiativeDecision (spec §4.3 step 4)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog

from mavis.bus.base import EventBus
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent, WakeupRequest
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.messages import TAINT_SUFFIX, Button, Outbound, Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import subjects
from mavis.initiative.composer import Composer
from mavis.initiative.quiet import QuietTracker
from mavis.initiative.subjects import Subject, SubjectKind, SubjectState, event_subject
from mavis.loops.service import LoopService
from mavis.policy.pings import (
    SECURITY_BYPASS_PREFIX,
    PingPolicy,
    in_quiet_hours,
    loop_ping_key,
    subject_ping_key,
)
from mavis.store.db import Session
from mavis.store.repo import messages, outbox
from mavis.timers.service import WakeupService

log = structlog.get_logger()

MAX_UNTRUSTED_URGENCY = 4  # only a trusted origin may bypass quiet hours (urgency 5)
DELAY_NOTE_AFTER = timedelta(minutes=30)
RELEASE_DELAY = timedelta(seconds=20)  # let the user's reply go out first
DEFERRED_TTL = timedelta(hours=12)  # a deferred ping with no better bound goes stale after this
SECURITY_DEFER_GRACE = timedelta(hours=2)
MERGE_WINDOW = timedelta(minutes=30)  # a model wakeup this close to one already set for the loop merges
CHAIN_LOOKBACK = timedelta(days=30)
LOOP_WAKEUP_KINDS = (WakeupKind.EVENT_STARTING, WakeupKind.EVENT_ENDED, WakeupKind.AGENT)
REMINDER_PREFIX = "Reminder the user asked for: "  # wake_me's wakeup reason
REMINDER_URGENCY = 4
LATE_REMINDER_AFTER = timedelta(hours=2)  # later than this, the reminder says it is late
UNTRUSTED_FIELDS = frozenset({"status", "entities", "watch"})  # all third-party content may change


def reminder_text(reason: str, due: datetime, now: datetime, timezone: str) -> str:
    """Fixed reminder copy (no composer, so nothing can decide not to send it)."""
    what = " ".join(reason.split()) or "the thing you asked me to remind you about"
    if now - timeutil.ensure_utc(due) <= LATE_REMINDER_AFTER:
        return f"⏰ Reminder: {what}"
    local_due, local_now = timeutil.to_local(due, timezone), timeutil.to_local(now, timezone)
    when = f"{local_due:%H:%M}" if local_due.date() == local_now.date() else f"{local_due:%a %d %b, %H:%M}"
    return f"⏰ Reminder, a bit late (it was for {when}): {what}"


class InitiativeExecutor:
    def __init__(self, bus: EventBus, loops: LoopService, wakeups: WakeupService, policy: PingPolicy,
                 composer: Composer, quiet: QuietTracker) -> None:
        self._bus, self._loops, self._wakeups = bus, loops, wakeups
        self._policy, self._composer, self._quiet = policy, composer, quiet

    async def apply(self, user, decision: InitiativeDecision, event: Event, context: str = "",
                    quiet_streak: int = 0, origin: dict[str, Any] | None = None,
                    evidence_loop_ids: frozenset[int] = frozenset()) -> None:
        """`evidence_loop_ids`: loops the triggering signal is code-matched to (an external signal that
        hit the loop's watch). Only those may be closed by the reasoner."""
        untrusted = event.trust is Trust.UNTRUSTED  # spec 8.3: third-party content may not create work
        # what the reasoner writes is as trusted as the least trusted thing it read
        write_trust = Trust.UNTRUSTED if untrusted or decision.tainted else Trust.SYSTEM
        # each wakeup's subject as it was before this run's own writes (a run's own edit is no change)
        bound = [(w, await self._subject_for(user.id, w, event)) for w in decision.wakeups]
        for upsert in decision.track:
            upsert = _without_unproven_closure(upsert, evidence_loop_ids, event)
            if upsert is None:
                continue
            if untrusted and not await self._owns_existing_loop(user.id, upsert.id):
                log.warning("initiative.untrusted_track_skipped", event_id=event.id,
                            title=(upsert.title or "")[:80])
                continue
            try:
                if untrusted:
                    upsert = await self._limit_untrusted_update(upsert, event)
                    if upsert is None:
                        continue
                # Provenance is set here, never taken from the model. An update by id is partial: what
                # the model did not set stays unchanged, so a status-only change never touches content
                # or trust.
                await self._loops.upsert(user.id, upsert.model_copy(update={
                    "source": event.id[:200], "trust": write_trust, "origin": LoopOrigin.REASONER}))
            except ValueError as exc:  # e.g. the LLM named a loop id that does not exist: not retryable
                log.warning("initiative.track_failed", event_id=event.id, title=(upsert.title or "")[:80],
                            error=str(exc))
        for w, state in bound:
            await self._schedule_bound(user, w, state, event, write_trust)
        for i, task in enumerate(decision.act):
            if untrusted:
                log.warning("initiative.untrusted_act_skipped", event_id=event.id, index=i)
                continue
            if not get_settings().initiative_act_enabled:
                log.info("initiative.act_disabled", event_id=event.id, index=i, goal=task.goal[:80])
                continue
            from mavis.agents.task_dispatch import dispatch_task_requests  # lazy: avoid an import cycle
            from mavis.domain.tasks import TaskOrigin

            # always tainted: the reasoner's prompt carries untrusted history, memory and email
            await dispatch_task_requests(user.id, [task], TaskOrigin.INITIATIVE, bus=self._bus,
                                         tainted=True)
        if decision.notify is not None:
            intent = decision.notify
            if intent.dedupe_key is None:  # retry-safe default: one notification per source event
                intent = intent.model_copy(update={"dedupe_key": f"notify:{event.id}"})
            await self.notify(user, intent, context=context, quiet_streak=quiet_streak,
                              untrusted=untrusted, original_due=timeutil.ensure_utc(event.occurred_at),
                              origin=origin)
        elif decision.ignore_reason:
            log.info("initiative.ignored", event_id=event.id, reason=decision.ignore_reason)

    async def release_deferred(self, user) -> int:
        """The user wrote during quiet hours, so they are awake: make their deferred pings due shortly.

        Each one still goes through the handler's origin check and notify(), so budget, dedupe and
        send-time revalidation apply. Outside quiet hours, deferrals are budget-driven and stay put.
        """
        now = timeutil.now()
        s = get_settings()
        local = timeutil.to_local(now, user.timezone)
        if not in_quiet_hours(local.hour, s.quiet_start, s.quiet_end) or s.quiet_awake_window_min <= 0:
            return 0
        due = now + RELEASE_DELAY
        moved = 0
        # attention asks and notifies deferred by quiet hours too; each is revalidated when it fires
        for kind in (WakeupKind.DEFERRED, WakeupKind.SYSTEM_ATTENTION_SPEAK):
            for w in await self._wakeups.pending(user.id, kind):
                if w.due_at > due and await self._wakeups.reschedule(w.id, due):
                    moved += 1
        if moved:
            log.info("initiative.deferred_released", user=user.id, count=moved)
        return moved

    async def remind(self, user, reason: str, dedupe_key: str, due: datetime) -> bool:
        """Deliver a reminder the user asked for. It always fires: fixed text, not the composer; the daily
        budget does not apply; quiet hours defer it (unless the user is awake) to a DEFERRED wakeup that
        comes back here, so writing during quiet hours releases it; late ones say so."""
        now = timeutil.now()
        verdict = await self._policy.check(user, REMINDER_URGENCY, dedupe_key, now, reminder=True)
        if not verdict.allow:
            log.info("initiative.reminder_held", user=user.id, reason=verdict.reason,
                     defer_until=verdict.defer_until)
            if verdict.defer_until is not None:
                await self._wakeups.wake_me(
                    user.id, verdict.defer_until, f"{REMINDER_PREFIX}{reason}", kind=WakeupKind.DEFERRED,
                    payload={"reminder_key": dedupe_key,
                             "original_due": timeutil.ensure_utc(due).isoformat()},
                    scale=False, reminder=True, dedupe_key=f"deferred:{dedupe_key}",
                )
            return False
        text = reminder_text(reason, due, now, user.timezone)
        await self.deliver(user, [text], dedupe_key, REMINDER_URGENCY)
        return True

    async def _subject_for(self, user_id: int, w: WakeupRequest, event: Event) -> SubjectState | None:
        """The subject a model wakeup is about, by explicit id only: the one the model named if it is
        really this user's, else the signal's own subject. Never guessed from the reason's words."""
        named = Subject.of(w.subject_kind, w.subject_id)
        if named is None and w.loop_id is not None:
            named = Subject(SubjectKind.LOOP, w.loop_id)
        if named is not None and (state := await subjects.resolve(user_id, named)) is not None:
            return state
        own = event_subject(event)  # the model named none, or an id that is not this user's
        return await subjects.resolve(user_id, own) if own is not None else None

    async def _schedule_bound(self, user, w: WakeupRequest, state: SubjectState | None, event: Event,
                              write_trust: Trust) -> None:
        if state is None or not state.live:
            log.warning("initiative.wakeup_unbound_rejected", event_id=event.id, reason=w.reason[:80],
                        subject=state.subject.key if state else None)
            return
        if event.source == "timer" and await self._rearm_without_change(user.id, state):
            log.info("initiative.wakeup_chain_stopped", event_id=event.id, subject=state.subject.key)
            return
        loop_id = state.loop_id
        if loop_id is not None and await self._covered(user.id, loop_id, w.at):
            log.info("initiative.wakeup_merged", event_id=event.id, loop_id=loop_id, reason=w.reason[:80])
            return
        # spec 8.3: a wakeup asked for by an untrusted event or by a run whose prompt carried untrusted
        # content, or one about an untrusted subject, fires as untrusted (scrubbed, capped)
        payload: dict[str, Any] = {"subject": state.subject.key, "subject_state": state.fingerprint}
        if write_trust is Trust.UNTRUSTED or state.untrusted:
            payload["untrusted"] = True
        try:
            await self._wakeups.wake_me(user.id, w.at, w.reason, loop_id, WakeupKind.AGENT,
                                        dedupe_key=f"agent:{state.subject.key}:{w.reason[:60]}",
                                        payload=payload)
        except ValueError as exc:
            log.warning("initiative.wakeup_failed", event_id=event.id, reason=w.reason[:80], error=str(exc))

    async def _rearm_without_change(self, user_id: int, state: SubjectState) -> bool:
        """A run started by a timer may not set another wakeup for a subject whose state is the same as
        when an earlier wakeup for it was set: no self-continuing chains (at most one re-check per
        state of the subject)."""
        since = timeutil.now() - CHAIN_LOOKBACK
        mark = (state.subject.key, state.fingerprint)
        return any((w.payload.get("subject"), w.payload.get("subject_state")) == mark
                   for w in await self._wakeups.history(user_id, WakeupKind.AGENT, since))

    async def _covered(self, user_id: int, loop_id: int, at: datetime) -> bool:
        """A pending wakeup for the same loop within MERGE_WINDOW already covers this one."""
        at = timeutil.ensure_utc(at)
        now = timeutil.now()
        if at > now:
            at = now + timeutil.scale_offset(at - now)  # compare like wake_me stores it
        return any(
            p.loop_id == loop_id and p.kind in LOOP_WAKEUP_KINDS and abs(p.due_at - at) <= MERGE_WINDOW
            for p in await self._wakeups.pending(user_id)
        )

    async def _limit_untrusted_update(self, upsert: LoopUpsert, event: Event) -> LoopUpsert | None:
        """Third-party content may change a loop's status, entities and watch, never when it is due or how
        important it is (that would let an email schedule a trusted, quiet-hours-bypassing nudge). The
        write carries untrusted trust, so a content change taints the loop and its re-planned wakeups."""
        current = await self._loops.get(upsert.id) if upsert.id is not None else None
        if current is None:
            return None
        allowed = upsert.model_dump(include={"id", *(UNTRUSTED_FIELDS & upsert.model_fields_set)})
        return LoopUpsert.model_validate(allowed).model_copy(update={
            "source": event.id[:200], "trust": Trust.UNTRUSTED})

    async def _loop_trusted(self, loop_id: int | None) -> bool:
        """A wakeup with no loop has no loop provenance to inherit."""
        if loop_id is None:
            return True
        loop = await self._loops.get(loop_id)
        return loop is None or loop.trusted

    async def _owns_existing_loop(self, user_id: int, loop_id: int | None) -> bool:
        if loop_id is None:
            return False
        loop = await self._loops.get(loop_id)
        return loop is not None and loop.user_id == user_id

    async def notify(self, user, intent: NotifyIntent, context: str = "", quiet_streak: int = 0,
                     untrusted: bool = False, original_due: datetime | None = None,
                     origin: dict[str, Any] | None = None,
                     buttons: list[list[Button]] | None = None) -> bool:
        if untrusted and intent.urgency > MAX_UNTRUSTED_URGENCY:
            intent = intent.model_copy(update={"urgency": MAX_UNTRUSTED_URGENCY})
        # an untrusted ping must not use up the loop's daily slot for this kind of ping
        o = origin or {}
        loop_key = None if untrusted else loop_ping_key(o.get("loop_id"), o.get("kind"))
        extra = [loop_key] if loop_key else []
        slot = subject_ping_key(o.get("subject"), o.get("kind"), untrusted)
        verdict = await self._policy.check(user, intent.urgency, intent.dedupe_key, timeutil.now(),
                                           extra_keys=[*extra, slot] if slot else extra,
                                           bypass_budget=intent.security)
        if not verdict.allow:
            log.info("initiative.notify_blocked", user=user.id, reason=verdict.reason,
                     defer_until=verdict.defer_until)
            if verdict.defer_until is not None:
                due = original_due or timeutil.now()
                valid_until = (origin or {}).get("valid_until") or (due + DEFERRED_TTL).isoformat()
                if intent.security:  # a capped security notice waits for the morning: still valid then
                    floor = timeutil.ensure_utc(verdict.defer_until) + SECURITY_DEFER_GRACE
                    current = timeutil.ensure_utc(datetime.fromisoformat(valid_until))
                    valid_until = max(current, floor).isoformat()
                await self._wakeups.wake_me(
                    user.id, verdict.defer_until, f"deferred: {intent.intent[:80]}", _loop_id(origin),
                    kind=WakeupKind.DEFERRED,
                    payload={"notify": intent.model_dump(mode="json"), "untrusted": untrusted,
                             "original_due": due.isoformat(), "origin": origin, "valid_until": valid_until},
                    scale=False,
                    dedupe_key=f"deferred:{intent.dedupe_key}" if intent.dedupe_key else None,
                )
            return False
        if verdict.budget_bypass:  # counts toward the daily cap on over-budget security notices
            extra = [*extra, f"{SECURITY_BYPASS_PREFIX}{intent.dedupe_key or timeutil.now().isoformat()}"]
        if intent.dedupe_key and await self._recover_partial(user, intent, tainted=untrusted):
            await self._follow_up_sent(origin)
            return False
        # one ping per subject per local day: taken atomically before composing, so two triggers about
        # the same thing (under different model keys) cannot both go out; given back if nothing is sent
        reserved = await self._policy.reserve(user, slot, timeutil.now()) if slot else None
        if slot and reserved is None:
            log.info("initiative.notify_subject_taken", user=user.id, slot=slot)
            return False
        try:
            message = await self._composer.compose(user, intent.intent, intent.urgency,
                                                    _with_delay_note(context, original_due, user),
                                                    untrusted=untrusted,
                                                    subject_record=await _subject_record(user.id, o))
        except BaseException:
            if reserved:
                await self._policy.release(user, reserved)
            raise
        if not message.send:
            log.info("initiative.composer_dropped", user=user.id, intent=intent.intent[:80])
            if reserved:
                await self._policy.release(user, reserved)
            return False
        await self.deliver(user, message.messages, intent.dedupe_key, intent.urgency, quiet_streak,
                           extra_keys=extra, buttons=buttons, tainted=untrusted,
                           quiet_subject=Subject.parse(o.get("subject")))
        await self._follow_up_sent(origin)
        return True

    async def _follow_up_sent(self, origin: dict[str, Any] | None) -> None:
        """A "how did it go?" was delivered: its loop now waits for the user's answer."""
        loop_id = _loop_id(origin)
        if loop_id is None or (origin or {}).get("kind") != EventType.EVENT_ENDED.value:
            return
        loop = await self._loops.get(loop_id)
        if loop is not None and loop.status is LoopStatus.OPEN:
            await self._loops.close(loop_id, LoopStatus.AWAITING_REPLY)

    async def _recover_partial(self, user, intent: NotifyIntent, tainted: bool = False) -> bool:
        """A prior attempt enqueued bubbles but died before log/record: finish that, send nothing new."""
        now = timeutil.now()
        local_date = timeutil.to_local(now, user.timezone).date().isoformat()
        sent = await outbox.texts_with_dedupe_prefix(f"{intent.dedupe_key}:{local_date}:")
        if not sent:
            return False
        await messages.log(user.id, Role.ASSISTANT, "\n".join(sent), proactive=True,
                           event_id=proactive_event_id(f"{intent.dedupe_key}:{local_date}:0", tainted))
        await self._policy.record(user, intent.dedupe_key, intent.urgency, now)
        log.info("initiative.notify_recovered", user=user.id, key=intent.dedupe_key)
        return True

    async def deliver(self, user, bubbles: list[str], dedupe_key: str | None = None, urgency: int = 3,
                      quiet_streak: int = 0, extra_keys: list[str] | None = None,
                      buttons: list[list[Button]] | None = None, tainted: bool = False,
                      quiet_subject: Subject | None = None) -> None:
        """`tainted`: the text was derived from third-party content. Its history row carries the taint
        marker, so the user's next reply is learned as untrusted (simple_turn._previous_tainted)."""
        now = timeutil.now()
        local_date = timeutil.to_local(now, user.timezone).date().isoformat()

        def scoped(prefix: str, i: int) -> str | None:
            return f"{prefix}{dedupe_key}:{local_date}:{i}" if dedupe_key else None

        async with Session() as session:
            last = len(bubbles) - 1
            for i, text in enumerate(bubbles):
                rows = buttons if (buttons and i == last) else []  # the keyboard rides on the last bubble
                await outbox.enqueue(session, Outbound(user_id=user.id, text=text, proactive=True,
                                                       dedupe_key=scoped("", i), buttons=rows))
            await session.commit()
        key = scoped("", 0)
        await messages.log(user.id, Role.ASSISTANT, "\n".join(bubbles), proactive=True,
                           event_id=proactive_event_id(key, tainted) if key else None)
        await self._policy.record(user, dedupe_key, urgency, now, extra_keys=extra_keys or ())
        if quiet_streak > 0:  # only a USER_QUIET nudge continues its chain; other proactive never arm one
            await self._quiet.after_assistant_message(user.id, bubbles[-1], streak=quiet_streak,
                                                      subject=quiet_subject)


CLOSING_STATUSES = frozenset({LoopStatus.DONE, LoopStatus.DROPPED, LoopStatus.EXPIRED})


def _without_unproven_closure(upsert: LoopUpsert, evidence: frozenset[int],
                              event: Event) -> LoopUpsert | None:
    """The reasoner may not close a loop on its own say-so: a closure without code evidence becomes a
    note in the log and the rest of the update still applies. A new loop born closed is skipped (it
    records a belief that something already happened). Phase B: the ledger turns such claims into item
    notes; until then the decision row keeps the claim."""
    if upsert.status not in CLOSING_STATUSES or "status" not in upsert.model_fields_set:
        return upsert
    if upsert.id is None:
        log.info("initiative.closed_create_skipped", event_id=event.id, title=(upsert.title or "")[:80],
                 status=upsert.status.value)
        return None
    if upsert.id in evidence:
        return upsert
    log.info("initiative.closure_claim_noted", event_id=event.id, loop_id=upsert.id,
             status=upsert.status.value)
    rest = upsert.model_fields_set - {"status"}
    if rest <= {"id", "kind", "title"}:  # kind/title echoed beside the closure are not an edit
        return None
    return LoopUpsert.model_validate(upsert.model_dump(include=rest))


def proactive_event_id(key: str, tainted: bool) -> str:
    return f"proactive:{key}{TAINT_SUFFIX if tainted else ''}"


def _loop_id(origin: dict[str, Any] | None) -> int | None:
    raw = (origin or {}).get("loop_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def _subject_record(user_id: int, origin: dict[str, Any]) -> str:
    """The source record of the ping's subject, read from its row now (never from history)."""
    subject = Subject.parse(origin.get("subject"))
    state = await subjects.resolve(user_id, subject) if subject is not None else None
    return state.record if state is not None else ""


def _with_delay_note(context: str, original_due: datetime | None, user) -> str:
    """Tell the composer when this message is late, so it does not talk as if it were still on time."""
    if original_due is None:
        return context
    now = timeutil.now()
    delay = now - timeutil.ensure_utc(original_due)
    if delay < DELAY_NOTE_AFTER:
        return context
    due_local = timeutil.to_local(original_due, user.timezone)
    note = (f"Delay: this was meant to go out at {due_local:%A %H:%M} local time but is going out "
            f"about {int(delay.total_seconds() // 3600)}h {int(delay.total_seconds() // 60) % 60}m late. "
            "Do not say it is happening right now; acknowledge the timing naturally if it matters.")
    return f"{context}\n{note}" if context else note
