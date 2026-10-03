"""How the attention layer speaks (spec attention section 9).

Ask: deterministic text from computed facts plus Yes/No buttons; no LLM, no injection surface. Notify:
composed through the existing executor with untrusted=True (capped at urgency 4, output scrubbed); if
the LLM is unavailable, a deterministic notify_text goes out instead, so a notify is never lost to load.
Both pre-check PingPolicy so attention owns its own deferral and keeps its buttons."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

import structlog

from mavis.attention.counterparty import display_name
from mavis.attention.policy import LOOKALIKE, NOTIFY_SECURITY
from mavis.attention.sanitize import REMOVED, clean, domain_label
from mavis.attention.scheduling import schedule_once
from mavis.attention.schema import FLAG_LABELS, METHOD_LABELS, AttentionDecision, EmailKind, Verdict
from mavis.channels.formatting import verbatim
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.errors import LLMError
from mavis.domain.messages import Button
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.email_triage import SECURITY_INTENT
from mavis.policy.pings import SECURITY_BYPASS_PREFIX, PingPolicy
from mavis.timers.service import WakeupService
from mavis.tools.integrations.normalize import to_datetime

log = structlog.get_logger()

PREFIX = "at:"
YES, NO, MUTE, ALWAYS = "at:y:", "at:n:", "at:m:", "at:a:"
SENT, DEFERRED, DROPPED = "sent", "deferred", "dropped"
QUESTION = "Was this you?"
MAX_NOTIFY_URGENCY = 4  # composed notifies are untrusted: the executor caps them here
BULK_MARKERS = ("promotional", "social", "forums", "list_unsubscribe")
_ISO_CURRENCY = re.compile(r"[A-Z]{3}")


def is_bulk(obs: Any) -> bool:
    """Promotional, social or forums mail, or mail with List-Unsubscribe: never a security notice.
    Ingest records these markers in facts["bulk"]; a log-only DROPPED observation is bulk by definition."""
    facts = obs.facts or {}
    dropped = obs.verdict == Verdict.DROPPED.value or obs.status == "dropped"
    return dropped or any(m in (facts.get("bulk") or []) for m in BULK_MARKERS)


def is_security_obs(obs: Any) -> bool:
    """Deterministic security notice (mirrors the policy's classification): the kind, any notifying risk
    flag, or the lookalike code; never for bulk mail, which could otherwise abuse the budget bypass."""
    facts = obs.facts or {}
    flags = {str(f) for f in facts.get("risk_flags") or []}
    security = (
        obs.kind == EmailKind.SECURITY.value
        or bool(flags & {str(f) for f in NOTIFY_SECURITY})
        or LOOKALIKE in (facts.get("codes") or [])
    )
    return security and not is_bulk(obs)


def can_mute(obs: Any) -> bool:
    """"Don't tell me about these" is never offered or honoured for security notices or for money items
    that stood out (an ask, any anomaly code, or a stored anomaly score at the brief threshold or above)."""
    if is_security_obs(obs) or obs.kind == EmailKind.SECURITY.value:
        return False
    facts = obs.facts or {}
    if not facts.get("money"):
        return True
    anomaly = float(facts.get("anomaly") or 0.0)
    stood_out = bool(facts.get("codes")) or anomaly >= get_settings().attention_brief_threshold
    return obs.verdict != Verdict.ASK.value and not stood_out


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
    return f"attn:{obs.message_id}"[:150]  # the outbox key appends :date:index


def _when(occurred: Any, received: Any, tz: str) -> str:
    dt = to_datetime(occurred) or timeutil.ensure_utc(received)
    local = timeutil.to_local(dt, tz)
    today = timeutil.to_local(timeutil.now(), tz).date()
    return f"{local:%H:%M}" if local.date() == today else f"{local:%a %d %b}, {local:%H:%M}"


def _currency(raw: Any) -> str:
    """Only a plain 3-letter ISO code is shown; anything else is dropped, never scrubbed into the amount."""
    code = str(raw or "").strip().upper()
    return code if _ISO_CURRENCY.fullmatch(code) else ""


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
        amount = format_amount(float(money["amount"]), _currency(money.get("currency")))
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
        parts.append(f"What it asks of them: {clean(obs.action, 120)}.")
    if why:
        parts.append(f"Why it matters: {why}.")
    parts.append("Suggest they open Gmail directly for the details.")
    return " ".join(parts)


def notify_text(obs: Any) -> list[str]:
    """Fixed wording for a notify when the composer's LLM is unavailable: computed facts and the
    scrubbed summary only (the same text digests show), never raw email text."""
    facts = obs.facts or {}
    codes = facts.get("codes") or []
    if obs.kind == EmailKind.SECURITY.value or is_security_obs(obs):
        labels = [FLAG_LABELS[f] for f in facts.get("risk_flags") or [] if f in FLAG_LABELS]
        first = (
            f"Heads up: an email about your {domain_label(obs.sender_domain)} account reports "
            f"{labels[0] if labels else 'a security change'}."
        )
        tail = ("If that wasn't you, open Gmail or the site directly (not the email's links) "
                "and secure the account.")
    else:
        summary = clean(str(obs.summary or ""), 200)
        first = f"Heads up about an email: {verbatim(summary)}." if summary else (
            f"Heads up: an email from {domain_label(obs.sender_domain)} looks worth a look.")
        tail = "Open Gmail directly for the details."
    if LOOKALIKE in codes or "lookalike_domain" in codes:
        tail = ("The sender imitates an address you normally get mail from, so don't open its links, "
                "reply or call numbers from it. " + tail)
    return [f"{first} {tail}"]


class Speaker:
    def __init__(self, executor_of: Callable[[], Any], policy: PingPolicy, wakeups: WakeupService) -> None:
        self._executor_of, self._policy, self._wakeups = executor_of, policy, wakeups

    async def speak(self, user: Any, obs: Any, decision: AttentionDecision) -> str:
        key = dedupe_key(obs)
        ask = decision.verdict is Verdict.ASK
        urgency = decision.urgency if ask else min(decision.urgency, MAX_NOTIFY_URGENCY)
        sec = not ask and is_security_obs(obs)  # security notices bypass the daily budget (capped 2/day)
        # "Was this you?" asks share that capped bypass and its counter (final review I2)
        verdict = await self._policy.check(user, urgency, key, timeutil.now(), bypass_budget=ask or sec)
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
        if ask:
            bypass = [f"{SECURITY_BYPASS_PREFIX}{key}"] if verdict.budget_bypass else []
            await executor.deliver(
                user,
                ask_text(obs, user.timezone),
                key,
                decision.urgency,
                extra_keys=bypass,
                buttons=ask_buttons(obs.id),
                tainted=True,  # names a payee from the email: a reply must not teach it as trusted fact
            )
            log.info("attention.spoke", obs_id=obs.id, verdict="ask", urgency=decision.urgency)
            return SENT
        intent = NotifyIntent(
            urgency=max(1, urgency),
            intent=notify_intent(obs, decision),
            dedupe_key=key,
            security=sec,
        )
        buttons = mute_buttons(obs.id) if can_mute(obs) else None
        try:
            sent = await executor.notify(user, intent, context=obs.summary, untrusted=True, buttons=buttons)
        except LLMError as exc:
            # The composer could not phrase it (LLM busy). The policy already allowed this ping, so
            # send the fixed wording rather than lose it (prod: a security notice, obs 81).
            log.warning("attention.notify_fallback_text", obs_id=obs.id, error=type(exc).__name__)
            bypass = [f"{SECURITY_BYPASS_PREFIX}{key}"] if verdict.budget_bypass else []
            await executor.deliver(user, notify_text(obs), key, intent.urgency, extra_keys=bypass,
                                   buttons=buttons, tainted=True)
            sent = True
        log.info("attention.spoke", obs_id=obs.id, verdict="notify", urgency=intent.urgency, sent=sent)
        return SENT if sent else DROPPED
