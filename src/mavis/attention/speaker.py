"""How the attention layer speaks (spec attention section 9).

Ask: deterministic text from computed facts plus Yes/No buttons; no LLM, no injection surface. Notify:
composed through the existing executor with untrusted=True (capped at urgency 4, output scrubbed).
Both pre-check PingPolicy so attention owns its own deferral and keeps its buttons."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import structlog

from mavis.attention.counterparty import display_name
from mavis.attention.sanitize import REMOVED, clean, domain_label
from mavis.attention.scheduling import schedule_once
from mavis.attention.schema import FLAG_LABELS, METHOD_LABELS, AttentionDecision, EmailKind, Verdict
from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.messages import Button
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.email_triage import SECURITY_INTENT
from mavis.policy.pings import PingPolicy
from mavis.timers.service import WakeupService
from mavis.tools.integrations.normalize import to_datetime

log = structlog.get_logger()

PREFIX = "at:"
YES, NO, MUTE, ALWAYS = "at:y:", "at:n:", "at:m:", "at:a:"
SENT, DEFERRED, DROPPED = "sent", "deferred", "dropped"
QUESTION = "Was this you?"


def ask_buttons(obs_id: int) -> list[list[Button]]:
    return [
        [
            Button(label="Yes, that was me", data=f"{YES}{obs_id}"),
            Button(label="No, help me", data=f"{NO}{obs_id}"),
        ]
    ]


def mute_buttons(obs_id: int) -> list[list[Button]]:
    return [[Button(label="Don't tell me about these", data=f"{MUTE}{obs_id}")]]


def _indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail, parts = s[:-3], s[-3:], []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join([*parts, tail])


def format_amount(amount: float, currency: str) -> str:
    amount = round(amount, 2)
    whole = int(amount)
    paise = round((amount - whole) * 100)
    if currency == "INR":
        return f"₹{_indian(whole)}" + (f".{paise:02d}" if paise else "")
    number = f"{amount:,.0f}" if not paise else f"{amount:,.2f}"
    return f"{currency} {number}".strip()


def join_reasons(reasons: Iterable[str]) -> str:
    r = [x for x in reasons if x][:3]
    if len(r) <= 1:
        return r[0] if r else ""
    return ", ".join(r[:-1]) + " and " + r[-1]


def dedupe_key(obs: Any) -> str:
    return f"attn:{obs.message_id}"[:200]


def _when(occurred: Any, received: Any, tz: str) -> str:
    dt = to_datetime(occurred) or timeutil.ensure_utc(received)
    local = timeutil.to_local(dt, tz)
    today = timeutil.to_local(timeutil.now(), tz).date()
    return f"{local:%H:%M}" if local.date() == today else f"{local:%a %d %b}, {local:%H:%M}"


def _payee(key: Any) -> str:
    """The counterparty is third-party text: scrub it again here, never trust what was stored."""
    name = clean(display_name(str(key or "")), 40)
    return name if name and REMOVED not in name else "an unknown payee"


def ask_text(obs: Any, tz: str) -> list[str]:
    facts = obs.facts or {}
    money = facts.get("money")
    if money and obs.kind != EmailKind.SECURITY.value:
        method = str(money.get("method") or "other")
        via = f" via {METHOD_LABELS[method]}" if method in METHOD_LABELS and method != "other" else ""
        amount = format_amount(float(money["amount"]), clean(money.get("currency") or "", 8))
        payee = _payee(money.get("counterparty_key"))
        when = _when(money.get("occurred_at"), obs.received_at, tz)
        first = f"Quick check: an email says {amount} was debited to {payee}{via}, at {when}."
    else:
        labels = [FLAG_LABELS[f] for f in facts.get("risk_flags") or [] if f in FLAG_LABELS]
        first = (
            f"Quick check: an email about your {domain_label(obs.sender_domain)} account reports "
            f"{labels[0] if labels else 'a security change'}."
        )
    why = clean(join_reasons(obs.reasons or []), 300)
    return [f"{first} It stood out because it's {why}." if why else first, QUESTION]


def notify_intent(obs: Any, decision: AttentionDecision) -> str:
    why = join_reasons(decision.reasons or tuple(obs.reasons or ()))
    if obs.kind == EmailKind.SECURITY.value:
        return f"{SECURITY_INTENT} What stood out: {why}." if why else SECURITY_INTENT
    parts = [
        f"Give the user a short heads-up about a {obs.kind.replace('_', ' ')} email from "
        f"{domain_label(obs.sender_domain)}."
    ]
    if "lookalike_domain" in ((obs.facts or {}).get("codes") or []):
        parts.append(
            "Warn them the sender imitates an address they normally get mail from: they should not "
            "open links, reply or call numbers from it."
        )
    if obs.action:
        parts.append(f"What it asks of them: {obs.action}.")
    if why:
        parts.append(f"Why it matters: {why}.")
    parts.append("Suggest they open Gmail directly for the details.")
    return " ".join(parts)


class Speaker:
    def __init__(self, executor_of: Callable[[], Any], policy: PingPolicy, wakeups: WakeupService) -> None:
        self._executor_of, self._policy, self._wakeups = executor_of, policy, wakeups

    async def speak(self, user: Any, obs: Any, decision: AttentionDecision) -> str:
        key = dedupe_key(obs)
        verdict = await self._policy.check(user, decision.urgency, key, timeutil.now())
        if not verdict.allow:
            if verdict.reason == "duplicate":
                return SENT
            if verdict.defer_until is not None:
                await schedule_once(
                    self._wakeups,
                    user.id,
                    WakeupKind.SYSTEM_ATTENTION_SPEAK,
                    str(obs.id),
                    verdict.defer_until,
                )
                log.info("attention.deferred", obs_id=obs.id, reason=verdict.reason)
                return DEFERRED
            return DROPPED
        executor = self._executor_of()
        if decision.verdict is Verdict.ASK:
            await executor.deliver(
                user, ask_text(obs, user.timezone), key, decision.urgency, buttons=ask_buttons(obs.id)
            )
            log.info("attention.spoke", obs_id=obs.id, verdict="ask", urgency=decision.urgency)
            return SENT
        intent = NotifyIntent(
            urgency=max(1, min(decision.urgency, 4)), intent=notify_intent(obs, decision), dedupe_key=key
        )
        buttons = None if obs.kind == EmailKind.SECURITY.value else mute_buttons(obs.id)
        sent = await executor.notify(user, intent, context=obs.summary, untrusted=True, buttons=buttons)
        log.info("attention.spoke", obs_id=obs.id, verdict="notify", urgency=intent.urgency, sent=sent)
        return SENT if sent else DROPPED
