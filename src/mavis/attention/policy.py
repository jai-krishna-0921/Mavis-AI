"""Attention decision (spec attention section 8): deterministic, explainable, no LLM."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from mavis.attention.index import PrefHit
from mavis.attention.schema import (
    LADDER,
    AnomalyResult,
    AttentionDecision,
    Direction,
    EmailKind,
    EmailUnderstanding,
    Feedback,
    RiskFlag,
    UrgencyHint,
    Verdict,
)
from mavis.config import Settings

BASE: dict[EmailKind, float] = {
    EmailKind.SECURITY: 0.55,
    EmailKind.DEADLINE_OR_BILL: 0.4,
    EmailKind.REQUEST_FROM_PERSON: 0.4,
    EmailKind.TRAVEL: 0.3,
    EmailKind.MONEY_MOVEMENT: 0.25,
    EmailKind.ACCOUNT_UPDATE: 0.15,
    EmailKind.OTHER: 0.1,
    EmailKind.RECEIPT_OR_ORDER: 0.05,
    EmailKind.NEWSLETTER: 0.0,
}
SOON = timedelta(hours=48)
ESCALATE_SCORE = 0.8
ESCALATE_MIN_REASONS = 2
URGENT_SECURITY = frozenset({RiskFlag.CREDENTIAL_CHANGE, RiskFlag.MFA_CHANGE})
NOTIFY_SECURITY = frozenset(
    {RiskFlag.NEW_SIGNIN, RiskFlag.CREDENTIAL_CHANGE, RiskFlag.MFA_CHANGE, RiskFlag.ACCOUNT_LOCKED}
)
LOOKALIKE = "lookalike_domain"
SENSITIVE_OFFSET_CAP = 0.1  # money and security kinds can never be tuned down further than this


@dataclass(frozen=True)
class PolicyInputs:
    understanding: EmailUnderstanding
    anomaly: AnomalyResult
    novelty: float
    now: datetime
    prefs: tuple[PrefHit, ...] = ()
    offset: float = 0.0  # learned per-kind offset: positive means "tell me less"
    sender_established: bool = False
    urgent_used_today: bool = False
    sender_authenticated: bool = False  # DMARC or DKIM pass for the From domain, captured at ingest
    sender_prior_security: bool = False  # this sender has sent SECURITY-kind mail before


def is_security(u: EmailUnderstanding) -> bool:
    """The kind and flags come from a model reading untrusted text: either one marks it security."""
    return u.kind is EmailKind.SECURITY or bool(set(u.risk_flags) & NOTIFY_SECURITY)


def _is_money(u: EmailUnderstanding) -> bool:
    return u.kind is EmailKind.MONEY_MOVEMENT or u.money is not None


def effective_offset(i: PolicyInputs) -> float:
    off = i.offset
    if off > SENSITIVE_OFFSET_CAP and (is_security(i.understanding) or _is_money(i.understanding)):
        return SENSITIVE_OFFSET_CAP
    return off


def pref_shift(hits: Iterable[PrefHit]) -> int:
    hits = list(hits)
    mute = math.fsum(sorted(h.score for h in hits if h.sentiment == Feedback.MUTE))
    always = math.fsum(sorted(h.score for h in hits if h.sentiment == Feedback.ALWAYS))
    return -1 if mute > always else 1 if always > mute else 0


def attention_score(i: PolicyInputs) -> float:
    u = i.understanding
    s = BASE[u.kind] + 0.6 * i.anomaly.score + 0.1 * i.novelty
    if u.needs_user:
        s += 0.15
    if u.urgency_hint is UrgencyHint.HIGH:
        s += 0.1
    elif u.urgency_hint is UrgencyHint.LOW:
        s -= 0.05
    if u.deadline is not None and timedelta(0) <= u.deadline - i.now <= SOON:
        s += 0.2
    return round(max(0.0, min(1.0, s - effective_offset(i))), 3)


def _shift(verdict: Verdict, step: int) -> Verdict:
    if verdict not in LADDER:
        return verdict
    return LADDER[max(0, min(len(LADDER) - 1, LADDER.index(verdict) + step))]


def decide(i: PolicyInputs, s: Settings) -> AttentionDecision:
    u, a = i.understanding, i.anomaly
    score = attention_score(i)
    debit = u.money is not None and u.money.direction is Direction.DEBIT and u.money.amount > 0
    flags = set(u.risk_flags)
    lookalike = LOOKALIKE in a.codes
    security = is_security(u)
    shift = pref_shift(i.prefs)
    # Money anomalies are judged on the RAW anomaly: offsets and preferences never lower them (C1).
    money_ask = debit and a.score >= s.attention_ask_threshold
    money_floor = _is_money(u) and a.score >= s.attention_brief_threshold
    if u.kind is EmailKind.NEWSLETTER and not lookalike and not security and not money_floor:
        return AttentionDecision(Verdict.BRIEF if shift > 0 else Verdict.LOG, 0, score, a.reasons)
    if lookalike:
        verdict = Verdict.NOTIFY  # imitation of a known sender: warn, never ask "was this you?"
    elif money_ask:
        verdict = Verdict.ASK
    elif u.kind is EmailKind.SECURITY and flags & URGENT_SECURITY and i.sender_established:
        verdict = Verdict.ASK
    elif security and (flags & NOTIFY_SECURITY or u.needs_user):
        verdict = Verdict.NOTIFY
    elif score >= s.attention_notify_threshold:
        verdict = Verdict.NOTIFY
    elif score >= s.attention_brief_threshold:
        verdict = Verdict.BRIEF
    else:
        verdict = Verdict.LOG
    protected = verdict is Verdict.ASK or security or lookalike or money_floor
    if shift < 0 and not protected:
        verdict = _shift(verdict, -1)
    elif shift > 0:
        verdict = _shift(verdict, +1)
    if (security or money_floor) and verdict is Verdict.LOG:
        verdict = Verdict.BRIEF  # floor: never silently logged, whatever the offset or preferences
    return AttentionDecision(verdict, _urgency(verdict, i, debit, flags, lookalike, s), score, a.reasons)


def _urgency(
    verdict: Verdict, i: PolicyInputs, debit: bool, flags: set[RiskFlag], lookalike: bool, s: Settings
) -> int:
    if verdict is Verdict.NOTIFY:
        return 4 if (is_security(i.understanding) or lookalike) else 3
    if verdict is not Verdict.ASK:
        return 0
    # Spec 8.4: urgency 5 (quiet hours bypass) only from deterministic facts, an established sender,
    # never a lookalike, at most once per local day.
    risky = (
        debit
        and i.anomaly.score >= ESCALATE_SCORE
        and len(i.anomaly.codes) >= ESCALATE_MIN_REASONS
        and i.sender_authenticated  # a spoof of an established bank address must not wake the user
    ) or (
        i.understanding.kind is EmailKind.SECURITY
        and bool(flags & URGENT_SECURITY)
        and i.sender_authenticated
        and i.sender_prior_security
    )
    escalate = (
        s.attention_allow_urgent
        and i.sender_established
        and not lookalike
        and not i.urgent_used_today
        and risky
    )
    return 5 if escalate else 4
