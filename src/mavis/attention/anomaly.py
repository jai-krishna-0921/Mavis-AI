"""Deterministic anomaly scoring (spec attention section 6.3). Each signal contributes a weight and a
human-readable reason; weights combine as a noisy-or into [0, 1]. Reasons never contain a domain,
address, link or raw counterparty text."""

from __future__ import annotations

import difflib
import math
from collections.abc import Iterable
from dataclasses import dataclass

from mavis.attention.baselines import MoneySnapshot, SenderStats
from mavis.attention.schema import FLAG_LABELS, NO_ANOMALY, AnomalyResult, Direction, RiskFlag

MIN_TYPICAL_COUNTERPARTY = 3
MIN_TYPICAL_OTHER = 5
MIN_HISTORY = 5
MIN_HOUR_SAMPLES = 10
RARE_HOUR_SHARE = 0.03
NIGHT_END = 6
LOOKALIKE_RATIO = 0.88
SECURITY_WEIGHTS: dict[RiskFlag, float] = {
    RiskFlag.CREDENTIAL_CHANGE: 0.6,
    RiskFlag.MFA_CHANGE: 0.6,
    RiskFlag.ASKS_FOR_CREDENTIALS: 0.5,
    RiskFlag.NEW_SIGNIN: 0.4,
    RiskFlag.ACCOUNT_LOCKED: 0.4,
    RiskFlag.ASKS_FOR_PAYMENT: 0.3,
    RiskFlag.PRESSURE_LANGUAGE: 0.2,
    RiskFlag.PAYMENT_FAILED: 0.2,
}

Part = tuple[str, float, str]


@dataclass(frozen=True)
class MoneyContext:
    amount: float
    direction: Direction
    method_label: str
    local_hour: int
    snapshot: MoneySnapshot
    recent_debits_1h: int = 0
    large_amount: float | None = None


def _result(parts: list[Part]) -> AnomalyResult:
    if not parts:
        return NO_ANOMALY
    keep = 1.0
    for _, weight, _ in parts:
        keep *= 1.0 - max(0.0, min(1.0, weight))
    return AnomalyResult(round(1.0 - keep, 3), tuple(p[0] for p in parts), tuple(p[2] for p in parts))


def combine(*results: AnomalyResult) -> AnomalyResult:
    keep, codes, reasons = 1.0, [], []
    for r in results:
        keep *= 1.0 - r.score
        codes += r.codes
        reasons += r.reasons
    return AnomalyResult(round(1.0 - keep, 3), tuple(codes), tuple(reasons)) if codes else NO_ANOMALY


def score_money(ctx: MoneyContext) -> AnomalyResult:
    if ctx.direction is not Direction.DEBIT or ctx.amount <= 0:
        return NO_ANOMALY
    snap, parts = ctx.snapshot, []
    typical, scope = 0.0, ""
    if snap.counterparty.count >= MIN_TYPICAL_COUNTERPARTY:
        typical, scope = snap.counterparty.median, "this payee"
    elif snap.method.count >= MIN_TYPICAL_OTHER:
        typical, scope = snap.method.median, f"{ctx.method_label} payments"
    elif snap.overall.count >= MIN_TYPICAL_OTHER:
        typical, scope = snap.overall.median, "your payments"
    if typical > 0:
        ratio = ctx.amount / typical
        if ratio >= 2:
            parts.append(
                ("amount_ratio", min(1.0, math.log10(ratio)), f"about {ratio:.0f}x your usual for {scope}")
            )
    elif ctx.large_amount is not None and ctx.amount >= ctx.large_amount:
        parts.append(("large_amount", 0.7, "a large amount, and I don't know your usual spending yet"))
    if snap.counterparty.count == 0:
        if snap.overall.count >= MIN_HISTORY:
            parts.append(("new_counterparty", 0.4, "the first payment I've seen to this payee"))
        else:
            parts.append(("new_counterparty", 0.15, "a payee I haven't seen before"))
    hours, h = snap.overall.hours, ctx.local_hour % 24
    total = sum(hours)
    if total >= MIN_HOUR_SAMPLES:
        near = hours[(h - 1) % 24] + hours[h] + hours[(h + 1) % 24]
        if near / total < RARE_HOUR_SHARE:
            parts.append(("odd_hour", 0.3, f"at {h:02d}:00, an hour you rarely pay at"))
    elif h < NIGHT_END:
        parts.append(("odd_hour", 0.25, f"at {h:02d}:00, in the middle of the night"))
    if ctx.recent_debits_1h >= 2:
        parts.append(("burst", 0.3, f"{ctx.recent_debits_1h + 1} payments within an hour"))
    return _result(parts)


def score_sender(stats: SenderStats, domain: str, established: set[str]) -> AnomalyResult:
    parts: list[Part] = []
    if stats.count == 0:
        parts.append(("new_sender", 0.2, "the first email I've seen from this sender"))
    if domain and domain not in established:
        if difflib.get_close_matches(domain, sorted(established), n=1, cutoff=LOOKALIKE_RATIO):
            parts.append(("lookalike_domain", 0.6, "the sender's address imitates one you get mail from"))
    return _result(parts)


def score_security(flags: Iterable[RiskFlag]) -> AnomalyResult:
    parts = [
        (f"risk:{f.value}", SECURITY_WEIGHTS[f], f"it mentions {FLAG_LABELS[f]}")
        for f in flags
        if f in SECURITY_WEIGHTS
    ]
    return _result(parts)
