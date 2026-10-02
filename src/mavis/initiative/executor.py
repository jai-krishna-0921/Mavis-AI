"""Apply an InitiativeDecision (spec §4.3 step 4)."""

from __future__ import annotations

import structlog

from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, Trust
from mavis.domain.messages import Outbound, Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.composer import Composer
from mavis.initiative.quiet import QuietTracker
from mavis.loops.service import LoopService
from mavis.policy.pings import PingPolicy
from mavis.store.db import Session
from mavis.store.repo import messages, outbox
from mavis.timers.service import WakeupService

log = structlog.get_logger()


class InitiativeExecutor:
    def __init__(self, bus: EventBus, loops: LoopService, wakeups: WakeupService, policy: PingPolicy,
                 composer: Composer, quiet: QuietTracker) -> None:
        self._bus, self._loops, self._wakeups = bus, loops, wakeups
        self._policy, self._composer, self._quiet = policy, composer, quiet

    async def apply(self, user, decision: InitiativeDecision, event: Event, context: str = "",
                    quiet_streak: int = 0) -> None:
        untrusted = event.trust is Trust.UNTRUSTED  # spec 8.3: third-party content may not create work
        for upsert in decision.track:
            if untrusted and not await self._owns_existing_loop(user.id, upsert.id):
                log.warning("initiative.untrusted_track_skipped", event_id=event.id, title=upsert.title[:80])
                continue
            await self._loops.upsert(user.id, upsert.model_copy(update={"source": upsert.source or event.id}))
        for i, w in enumerate(decision.wakeups):
            key = f"agent:{w.loop_id}:{w.reason[:60]}" if w.loop_id else f"agent:{event.id}:{i}"
            await self._wakeups.wake_me(user.id, w.at, w.reason, w.loop_id, WakeupKind.AGENT, dedupe_key=key)
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
                              untrusted=untrusted)
        elif decision.ignore_reason:
            log.info("initiative.ignored", event_id=event.id, reason=decision.ignore_reason)

    async def _owns_existing_loop(self, user_id: int, loop_id: int | None) -> bool:
        if loop_id is None:
            return False
        loop = await self._loops.get(loop_id)
        return loop is not None and loop.user_id == user_id

    async def notify(self, user, intent: NotifyIntent, context: str = "", quiet_streak: int = 0,
                     untrusted: bool = False) -> bool:
        verdict = await self._policy.check(user, intent.urgency, intent.dedupe_key, timeutil.now())
        if not verdict.allow:
            log.info("initiative.notify_blocked", user=user.id, reason=verdict.reason,
                     defer_until=verdict.defer_until)
            if verdict.defer_until is not None:
                await self._wakeups.wake_me(
                    user.id, verdict.defer_until, f"deferred: {intent.intent[:80]}", kind=WakeupKind.DEFERRED,
                    payload={"notify": intent.model_dump(mode="json"), "untrusted": untrusted}, scale=False,
                    dedupe_key=f"deferred:{intent.dedupe_key}" if intent.dedupe_key else None,
                )
            return False
        message = await self._composer.compose(user, intent.intent, intent.urgency, context,
                                                untrusted=untrusted)
        if not message.send:
            log.info("initiative.composer_dropped", user=user.id, intent=intent.intent[:80])
            return False
        await self.deliver(user, message.messages, intent.dedupe_key, intent.urgency, quiet_streak)
        return True

    async def deliver(self, user, bubbles: list[str], dedupe_key: str | None = None, urgency: int = 3,
                      quiet_streak: int = 0) -> None:
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
        await self._policy.record(user, dedupe_key, urgency, now)
        await self._quiet.after_assistant_message(user.id, bubbles[-1], streak=quiet_streak)
