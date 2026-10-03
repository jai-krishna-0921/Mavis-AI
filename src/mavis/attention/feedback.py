"""Button presses on attention messages (spec attention section 10).

The press is the user's own action (trust=USER), so the loop and follow-up it creates are trusted.
All text here is fixed copy or computed facts; nothing from the email is echoed back.

Concurrency and retries: a press is handled under a per-user lock (its own key, since the runner already
holds `user:{id}` for button turns), and every effect is guarded by the observation's stored feedback, so a
double tap or a redelivered BUTTON_PRESSED never counts twice. Learning effects (baseline, preference,
offset) are recorded after the feedback is marked, so a crash can lose one lesson but never double it. The
dispute is the opposite: its effects (loop, follow-up) are idempotent, so they run first and a retry
after a crash still opens them."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog

from mavis.attention.baselines import Baselines
from mavis.attention.index import AttentionIndex
from mavis.attention.learning import Thresholds
from mavis.attention.sanitize import domain_label
from mavis.attention.schema import EmailKind, Feedback
from mavis.attention.speaker import PREFIX, can_mute, format_amount
from mavis.domain import timeutil
from mavis.domain.events import Event
from mavis.domain.loops import LoopKind, LoopUpsert
from mavis.domain.messages import Outbound, Role
from mavis.domain.wakeups import WakeupKind
from mavis.loops.service import LoopService
from mavis.store.db import Session
from mavis.store.repo import attention as repo
from mavis.store.repo import messages, outbox, users
from mavis.timers.service import WakeupService
from mavis.worker.locks import lock

log = structlog.get_logger()

FOLLOW_UP = timedelta(hours=2)
MONEY_STEPS = [
    "Okay, let's deal with it now. Open your banking or payment app directly, not the email, and check "
    "whether this payment is really there.",
    "If it is and it wasn't you, block the card or payment method from the app and report it through the "
    "app or the official website, never through a link or number in the email. Reporting quickly usually "
    "limits what you can lose. I'll check back with you in 2 hours.",
]
SECURITY_STEPS = [
    "Okay. Go to that account's website or app directly, not through the email, and change your password.",
    "Then sign out other sessions and check your recovery email, phone and two-step settings. "
    "I'll check back with you in 2 hours.",
]
CONFIRMED_TEXT = "Thanks, noted. I'll treat payments like that as normal for you."
MUTE_TEXT = "Got it. I'll stop pinging you about emails like that, and still keep them in my log if you ask."
MUTE_REFUSED_TEXT = (
    "I'll keep telling you about emails like that, since they can involve your money or security."
)
ALWAYS_TEXT = "Will do. I'll always tell you about emails like that."


def _is_money(obs: Any) -> bool:
    return bool((obs.facts or {}).get("money")) and obs.kind != EmailKind.SECURITY.value


def next_steps(obs: Any) -> list[str]:
    return list(MONEY_STEPS if _is_money(obs) else SECURITY_STEPS)


def dispute_title(obs: Any, tz: str) -> str:
    day = timeutil.to_local(timeutil.ensure_utc(obs.received_at), tz)
    if _is_money(obs):
        m = obs.facts["money"]
        amount = format_amount(float(m["amount"]), str(m.get("currency") or ""))
        return f"Unrecognised debit of {amount} on {day:%d %b}: check it in your banking or payment app"
    return f"Unrecognised security change on your {domain_label(obs.sender_domain)} account: secure it"


async def _reply(event: Event, bubbles: list[str]) -> None:
    async with Session() as s:
        for i, text in enumerate(bubbles):
            await outbox.enqueue(
                s, Outbound(user_id=event.user_id, text=text, dedupe_key=f"reply:{event.id}:{i}")
            )
        await s.commit()
    await messages.log(event.user_id, Role.ASSISTANT, "\n".join(bubbles), event_id=f"reply:{event.id}")


class FeedbackHandler:
    def __init__(
        self,
        *,
        loops: LoopService,
        wakeups: WakeupService,
        baselines: Baselines,
        index: AttentionIndex,
        thresholds: Thresholds,
    ) -> None:
        self._loops, self._wakeups, self._baselines = loops, wakeups, baselines
        self._index, self._thresholds = index, thresholds

    async def on_button(self, event: Event, data: str) -> None:
        # The callback itself is acknowledged at ingest (telegram_updates.ingest_update), before this runs.
        try:
            action, raw = data.removeprefix(PREFIX).split(":", 1)
            obs_id = int(raw)
        except ValueError:
            log.warning("attention.bad_button", data=data[:40])
            return
        handlers = {"y": self._confirm, "n": self._dispute, "m": self._mute, "a": self._always}
        fn = handlers.get(action)
        if fn is None:
            log.warning("attention.button_ignored", action=action, obs_id=obs_id)
            return
        async with lock(f"attention-feedback:{event.user_id}"):
            obs = await repo.get(obs_id)  # read under the lock: a concurrent tap sees the first one's result
            if obs is None or obs.user_id != event.user_id:
                log.warning("attention.button_ignored", action=action, obs_id=obs_id)
                return
            await _reply(event, await fn(event, obs))
        log.info("attention.feedback", obs_id=obs.id, action=action, kind=obs.kind)

    async def _remember(self, obs: Any, sentiment: Feedback) -> None:
        pref_id = await repo.add_pref(obs.user_id, obs.id, obs.kind, sentiment.value, obs.summary)
        try:
            vector = await self._index.embed(obs.summary or obs.kind)
            await repo.set_pref_point(
                pref_id, await self._index.add_pref(obs.user_id, pref_id, sentiment.value, obs.kind, vector)
            )
        except Exception as exc:  # noqa: BLE001 - the preference row still counts for thresholds
            log.warning("attention.pref_embed_failed", error=type(exc).__name__)

    async def _confirm(self, event: Event, obs: Any) -> list[str]:
        if obs.feedback == Feedback.CONFIRMED.value:
            return [CONFIRMED_TEXT]
        facts = dict(obs.facts or {})
        money = facts.get("money")
        record = bool(money and money.get("direction") == "debit" and not facts.get("baselined"))
        if record:
            facts["baselined"] = True  # marked first: a crash can skip the baseline, never double it
        await repo.set_fields(obs.id, feedback=Feedback.CONFIRMED.value, facts=facts)
        if record:
            # An explicit user "Yes" may feed the baseline (poisoning ruling): it is the user's own act.
            user = await users.get(obs.user_id)
            received = timeutil.ensure_utc(obs.received_at)
            hour = timeutil.to_local(received, user.timezone).hour
            await self._baselines.record_money(
                obs.user_id,
                money["currency"],
                money.get("counterparty_key", ""),
                money.get("method", "other"),
                float(money["amount"]),
                hour,
                received,
            )
        await self._remember(obs, Feedback.CONFIRMED)
        await self._thresholds.learn(obs.user_id, obs.kind, Feedback.CONFIRMED)
        return [CONFIRMED_TEXT]

    async def _dispute(self, event: Event, obs: Any) -> list[str]:
        if obs.feedback != Feedback.DISPUTED.value:
            user = await users.get(obs.user_id)
            loop = await self._loops.upsert(
                obs.user_id,
                LoopUpsert(
                    kind=LoopKind.CONCERN,
                    title=dispute_title(obs, user.timezone),
                    importance=5,
                    source=event.id,
                ),
            )
            what = "payment" if _is_money(obs) else "security change"
            await self._wakeups.wake_me(
                obs.user_id,
                timeutil.now() + FOLLOW_UP,
                f"Follow up: did they secure the {what} they did not recognise?",
                loop.id,
                WakeupKind.AGENT,
                dedupe_key=f"attn:followup:{obs.id}",
                scale=False,
            )
            await repo.set_fields(obs.id, feedback=Feedback.DISPUTED.value)
            await self._thresholds.learn(obs.user_id, obs.kind, Feedback.DISPUTED)
        return next_steps(obs)

    async def _mute(self, event: Event, obs: Any) -> list[str]:
        if not can_mute(obs):  # a stale or forged button on a security or high-anomaly money item
            log.warning("attention.mute_refused", obs_id=obs.id, kind=obs.kind)
            return [MUTE_REFUSED_TEXT]
        if obs.feedback != Feedback.MUTE.value:
            await repo.set_fields(obs.id, feedback=Feedback.MUTE.value)
            await self._remember(obs, Feedback.MUTE)
            await self._thresholds.learn(obs.user_id, obs.kind, Feedback.MUTE)
        return [MUTE_TEXT]

    async def _always(self, event: Event, obs: Any) -> list[str]:
        if obs.feedback != Feedback.ALWAYS.value:
            await repo.set_fields(obs.id, feedback=Feedback.ALWAYS.value)
            await self._remember(obs, Feedback.ALWAYS)
            await self._thresholds.learn(obs.user_id, obs.kind, Feedback.ALWAYS)
        return [ALWAYS_TEXT]
