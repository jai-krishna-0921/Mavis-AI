"""Apply an InitiativeDecision (spec §4.3 step 4)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog

from mavis.bus.base import EventBus
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, Trust
from mavis.domain.messages import Outbound, Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.composer import Composer
from mavis.initiative.quiet import QuietTracker
from mavis.loops.service import LoopService
from mavis.policy.pings import PingPolicy, in_quiet_hours, loop_ping_key
from mavis.store.db import Session
from mavis.store.repo import messages, outbox
from mavis.timers.service import WakeupService

log = structlog.get_logger()

MAX_UNTRUSTED_URGENCY = 4  # only a trusted origin may bypass quiet hours (urgency 5)
DELAY_NOTE_AFTER = timedelta(minutes=30)
RELEASE_DELAY = timedelta(seconds=20)  # let the user's reply go out first


class InitiativeExecutor:
    def __init__(self, bus: EventBus, loops: LoopService, wakeups: WakeupService, policy: PingPolicy,
                 composer: Composer, quiet: QuietTracker) -> None:
        self._bus, self._loops, self._wakeups = bus, loops, wakeups
        self._policy, self._composer, self._quiet = policy, composer, quiet

    async def apply(self, user, decision: InitiativeDecision, event: Event, context: str = "",
                    quiet_streak: int = 0, origin: dict[str, Any] | None = None) -> None:
        untrusted = event.trust is Trust.UNTRUSTED  # spec 8.3: third-party content may not create work
        for upsert in decision.track:
            if untrusted and not await self._owns_existing_loop(user.id, upsert.id):
                log.warning("initiative.untrusted_track_skipped", event_id=event.id, title=upsert.title[:80])
                continue
            try:
                source = upsert.source or event.id
                await self._loops.upsert(user.id, upsert.model_copy(update={"source": source}))
            except ValueError as exc:  # e.g. the LLM named a loop id that does not exist: not retryable
                log.warning("initiative.track_failed", event_id=event.id, title=upsert.title[:80],
                            error=str(exc))
        for i, w in enumerate(decision.wakeups):
            key = f"agent:{w.loop_id}:{w.reason[:60]}" if w.loop_id else f"agent:{event.id}:{i}"
            try:
                await self._wakeups.wake_me(user.id, w.at, w.reason, w.loop_id, WakeupKind.AGENT,
                                            dedupe_key=key)
            except ValueError as exc:
                log.warning("initiative.wakeup_failed", event_id=event.id, reason=w.reason[:80],
                            error=str(exc))
        for i, task in enumerate(decision.act):
            if untrusted:
                log.warning("initiative.untrusted_act_skipped", event_id=event.id, index=i)
                continue
            # Placeholder until Phase 4 registers RUN_TASK dispatch: log and skip, enqueue nothing.
            log.info("initiative.act_skipped", event_id=event.id, index=i, goal=task.goal[:80])
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
        for w in await self._wakeups.pending(user.id, WakeupKind.DEFERRED):
            if w.due_at > due and await self._wakeups.reschedule(w.id, due):
                moved += 1
        if moved:
            log.info("initiative.deferred_released", user=user.id, count=moved)
        return moved

    async def _owns_existing_loop(self, user_id: int, loop_id: int | None) -> bool:
        if loop_id is None:
            return False
        loop = await self._loops.get(loop_id)
        return loop is not None and loop.user_id == user_id

    async def notify(self, user, intent: NotifyIntent, context: str = "", quiet_streak: int = 0,
                     untrusted: bool = False, original_due: datetime | None = None,
                     origin: dict[str, Any] | None = None) -> bool:
        if untrusted and intent.urgency > MAX_UNTRUSTED_URGENCY:
            intent = intent.model_copy(update={"urgency": MAX_UNTRUSTED_URGENCY})
        extra = [k for k in (loop_ping_key((origin or {}).get("loop_id"), (origin or {}).get("kind")),) if k]
        verdict = await self._policy.check(user, intent.urgency, intent.dedupe_key, timeutil.now(),
                                           extra_keys=extra)
        if not verdict.allow:
            log.info("initiative.notify_blocked", user=user.id, reason=verdict.reason,
                     defer_until=verdict.defer_until)
            if verdict.defer_until is not None:
                await self._wakeups.wake_me(
                    user.id, verdict.defer_until, f"deferred: {intent.intent[:80]}", kind=WakeupKind.DEFERRED,
                    payload={"notify": intent.model_dump(mode="json"), "untrusted": untrusted,
                             "original_due": (original_due or timeutil.now()).isoformat(), "origin": origin},
                    scale=False,
                    dedupe_key=f"deferred:{intent.dedupe_key}" if intent.dedupe_key else None,
                )
            return False
        if intent.dedupe_key and await self._recover_partial(user, intent):
            return False
        message = await self._composer.compose(user, intent.intent, intent.urgency,
                                                _with_delay_note(context, original_due, user),
                                                untrusted=untrusted)
        if not message.send:
            log.info("initiative.composer_dropped", user=user.id, intent=intent.intent[:80])
            return False
        await self.deliver(user, message.messages, intent.dedupe_key, intent.urgency, quiet_streak,
                           extra_keys=extra)
        return True

    async def _recover_partial(self, user, intent: NotifyIntent) -> bool:
        """A prior attempt enqueued bubbles but died before log/record: finish that, send nothing new."""
        now = timeutil.now()
        local_date = timeutil.to_local(now, user.timezone).date().isoformat()
        sent = await outbox.texts_with_dedupe_prefix(f"{intent.dedupe_key}:{local_date}:")
        if not sent:
            return False
        await messages.log(user.id, Role.ASSISTANT, "\n".join(sent), proactive=True,
                           event_id=f"proactive:{intent.dedupe_key}:{local_date}:0")
        await self._policy.record(user, intent.dedupe_key, intent.urgency, now)
        log.info("initiative.notify_recovered", user=user.id, key=intent.dedupe_key)
        return True

    async def deliver(self, user, bubbles: list[str], dedupe_key: str | None = None, urgency: int = 3,
                      quiet_streak: int = 0, extra_keys: list[str] | None = None) -> None:
        now = timeutil.now()
        local_date = timeutil.to_local(now, user.timezone).date().isoformat()

        def scoped(prefix: str, i: int) -> str | None:
            return f"{prefix}{dedupe_key}:{local_date}:{i}" if dedupe_key else None

        async with Session() as session:
            for i, text in enumerate(bubbles):
                await outbox.enqueue(session, Outbound(user_id=user.id, text=text, proactive=True,
                                                       dedupe_key=scoped("", i)))
            await session.commit()
        await messages.log(user.id, Role.ASSISTANT, "\n".join(bubbles), proactive=True,
                           event_id=scoped("proactive:", 0))
        await self._policy.record(user, dedupe_key, urgency, now, extra_keys=extra_keys or ())
        if quiet_streak > 0:  # only a USER_QUIET nudge continues its chain; other proactive never arm one
            await self._quiet.after_assistant_message(user.id, bubbles[-1], streak=quiet_streak)


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
