"""Per-email attention pipeline (spec attention section 4): understand, score, decide, persist, speak.

Runs under the per-user initiative lock (EMAIL_RECEIVED and system wakeups both take it), so one
observation is never processed twice at the same time."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from mavis.attention.anomaly import MoneyContext, combine, score_money, score_security, score_sender
from mavis.attention.baselines import Baselines, SenderStats
from mavis.attention.counterparty import match_key, normalize_counterparty
from mavis.attention.index import AttentionIndex, PrefHit
from mavis.attention.learning import Thresholds, offset_for
from mavis.attention.policy import LOOKALIKE, PolicyInputs, decide
from mavis.attention.sanitize import clean, summary_text
from mavis.attention.schema import (
    METHOD_LABELS,
    NO_ANOMALY,
    AnomalyResult,
    AttentionDecision,
    Direction,
    EmailKind,
    EmailUnderstanding,
    Verdict,
)
from mavis.attention.speaker import DEFERRED, SENT, Speaker
from mavis.attention.understand import Understander, heuristic
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType
from mavis.store.repo import attention as repo
from mavis.store.repo import users

log = structlog.get_logger()

ACTION_LIMIT = 160
BURST_WINDOW = timedelta(hours=1)
SPEAK_WINDOW = timedelta(hours=12)  # older live mail goes to the brief instead of a ping
DEFERRED_STALE = timedelta(hours=24)
SPEAKS = (Verdict.ASK, Verdict.NOTIFY)
URGENT = 5
BASELINE_DAILY_CAP = 3  # debits recorded per payee per local day (baseline poisoning ruling)
BULK_LABEL_MARKERS = {
    "CATEGORY_PROMOTIONS": "promotional",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_FORUMS": "forums",
}


def bulk_markers(payload: dict) -> list[str]:
    """facts["bulk"]: mail provider category labels and List-Unsubscribe. Bulk mail never gets the
    security budget bypass (speaker.is_bulk)."""
    labels = set(payload.get("labels") or [])
    out = [marker for label, marker in BULK_LABEL_MARKERS.items() if label in labels]
    if payload.get("list_unsubscribe"):
        out.append("list_unsubscribe")
    return out


HEURISTIC_MAX_URGENCY = 4


def cap_heuristic(d: AttentionDecision) -> AttentionDecision:
    """The keyword fallback runs exactly when the model misbehaves: it may notify, never ask "was this
    you?" and never reach urgency 5 (spec 5.4)."""
    if d.verdict is Verdict.ASK:
        return replace(d, verdict=Verdict.NOTIFY, urgency=HEURISTIC_MAX_URGENCY)
    return replace(d, urgency=min(d.urgency, HEURISTIC_MAX_URGENCY))


def localize_understanding(u: EmailUnderstanding, tz: str) -> EmailUnderstanding:
    """Model datetimes without an offset are the user's wall-clock time: make every one aware UTC before
    policy, anomaly scoring or text use them (a naive deadline crashed the policy; a naive occurred_at
    read as UTC shifted the odd-hour signal, the hour baseline and the ask text)."""
    update: dict[str, Any] = {}
    if u.deadline is not None:
        update["deadline"] = timeutil.to_utc(u.deadline, tz)
    if u.money is not None and u.money.occurred_at is not None:
        money = u.money.model_copy(update={"occurred_at": timeutil.to_utc(u.money.occurred_at, tz)})
        update["money"] = money
    return u.model_copy(update=update) if update else u


def local_day_start(now: datetime, tz: str) -> datetime:
    local = timeutil.to_local(now, tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


class AttentionPipeline:
    def __init__(
        self,
        *,
        understander: Understander,
        baselines: Baselines,
        index: AttentionIndex,
        speaker: Speaker,
        thresholds: Thresholds,
    ) -> None:
        self._understander, self._baselines, self._index = understander, baselines, index
        self._speaker, self._thresholds = speaker, thresholds

    async def process(self, user: Any, obs: Any) -> bool:
        """False when the email stays pending (LLM unavailable, attempts left); the drain retries it."""
        payload = dict(obs.pending_payload or {})
        await repo.note_attempt(obs.id, timeutil.now())
        method = "llm"
        try:
            understanding = await self._understander.understand(payload, user.timezone)
        except LLMError as exc:
            log.warning(
                "attention.understand_failed",
                obs_id=obs.id,
                attempt=obs.attempts + 1,
                error=type(exc).__name__,
            )
            if obs.attempts + 1 < get_settings().attention_max_attempts:
                return False
            understanding, method = heuristic(payload), "heuristic"
        await self.finalize(user, obs, payload, understanding, method)
        return True

    async def finalize_cheap(self, user: Any, obs: Any, payload: dict, verdict: Verdict) -> None:
        """Label-dropped or forwarded mail: logged without an LLM call or an embedding."""
        kind = EmailKind.NEWSLETTER if verdict is Verdict.DROPPED else EmailKind.OTHER
        summary = summary_text(kind, obs.sender_domain, str(payload.get("subject", "")))
        facts = {
            "bulk": bulk_markers(payload),
            "authenticated": bool(payload.get("sender_authenticated")),
        }
        established = await self._baselines.established_domains(user.id, timeutil.now())
        if await repo.finish(
            obs.id, method="labels", kind=kind.value, verdict=verdict.value, summary=summary, facts=facts
        ) and not self._lookalike(obs.sender_domain, established):
            await self._baselines.touch_sender(
                user.id,
                str(payload.get("from_address", "")),
                obs.sender_domain,
                timeutil.ensure_utc(obs.received_at),
            )
        log.info(
            "attention.observed",
            obs_id=obs.id,
            kind=kind.value,
            verdict=verdict.value,
            method="labels",
            origin=obs.origin,
        )

    async def finalize(
        self, user: Any, obs: Any, payload: dict, u: EmailUnderstanding, method: str
    ) -> AttentionDecision:
        s = get_settings()
        u = localize_understanding(u, user.timezone)
        now = timeutil.now()
        received = timeutil.ensure_utc(obs.received_at)
        address = str(payload.get("from_address", "")).lower()
        sender_before = await self._baselines.sender(user.id, address)
        established = await self._baselines.established_domains(user.id, now)
        sender_established = sender_before.established(now)
        lookalike = self._lookalike(obs.sender_domain, established)
        authenticated = bool(payload.get("sender_authenticated"))
        prior_security = await repo.prior_security(user.id, obs.sender_domain, obs.id)
        money, money_result = await self._money(user, obs, u, received)
        sender_result = (
            NO_ANOMALY
            if u.kind is EmailKind.NEWSLETTER
            else score_sender(sender_before, obs.sender_domain, established)
        )
        security_result = score_security(u.risk_flags) if u.kind is EmailKind.SECURITY else NO_ANOMALY
        anomaly = combine(money_result, sender_result, security_result)
        summary = summary_text(u.kind, obs.sender_domain, str(payload.get("subject", "")))
        vector, novelty, prefs = await self._embedding_signals(user.id, summary)
        state = await self._thresholds.load(user.id)
        local_day = timeutil.to_local(now, user.timezone).date().isoformat()
        decision = decide(
            PolicyInputs(
                understanding=u,
                anomaly=anomaly,
                novelty=novelty,
                now=now,
                prefs=tuple(prefs),
                offset=offset_for(state, u.kind.value),
                sender_established=sender_established,
                urgent_used_today=state.get("urgent_day") == local_day,
                sender_authenticated=authenticated,
                sender_prior_security=prior_security,
            ),
            s,
        )
        if method == "heuristic":
            decision = cap_heuristic(decision)
        speak = (
            decision.verdict in SPEAKS and obs.origin == repo.ORIGIN_LIVE and now - received <= SPEAK_WINDOW
        )
        record = await self._may_record(user, money, decision, sender_established, lookalike, received)
        point_id = await self._index_observation(user.id, obs.id, u.kind.value, vector, received)
        facts = {
            "money": money,
            "deadline": u.deadline.isoformat() if u.deadline else None,
            "risk_flags": [f.value for f in u.risk_flags],
            "people": len(u.people),
            "urgency_hint": u.urgency_hint.value,
            "codes": list(anomaly.codes),
            "anomaly": anomaly.score,  # raw score: can_mute and the money floor read it, never offset
            "bulk": bulk_markers(payload),
            "authenticated": authenticated,
            "baselined": record,
        }
        done = await repo.finish(
            obs.id,
            method=method,
            kind=u.kind.value,
            needs_user=u.needs_user,
            verdict=decision.verdict.value,
            urgency=decision.urgency,
            score=anomaly.score,
            reasons=list(anomaly.reasons),
            facts=facts,
            summary=summary,
            action=clean(u.action_requested, ACTION_LIMIT),
            point_id=point_id,
            delivery=repo.QUEUED if speak else "none",
        )
        if not done:
            return decision
        if not lookalike:  # an imitation of a known sender never becomes established itself
            await self._baselines.touch_sender(user.id, address, obs.sender_domain, received)
        if record:
            await self._baselines.record_money(
                user.id,
                money["currency"],
                money["counterparty_key"],
                money["method"],
                money["amount"],
                money["local_hour"],
                received,
            )
        log.info(
            "attention.observed",
            obs_id=obs.id,
            kind=u.kind.value,
            verdict=decision.verdict.value,
            method=method,
            score=anomaly.score,
            attention=decision.attention,
            origin=obs.origin,
        )
        if speak and (fresh := await repo.get(obs.id)) is not None:
            await self.deliver_queued(user, fresh, decision)
        return decision

    @staticmethod
    def _lookalike(domain: str, established: set[str]) -> bool:
        """Independent of the kind (newsletters skip sender scoring): close to, but not, a known domain."""
        return LOOKALIKE in score_sender(SenderStats(count=1), domain, established).codes

    async def _may_record(
        self,
        user: Any,
        money: dict | None,
        decision: AttentionDecision,
        sender_established: bool,
        lookalike: bool,
        received: datetime,
    ) -> bool:
        """Baseline poisoning ruling: only debits from an established, non-lookalike sender that were not
        held as an ask feed the money baseline, at most BASELINE_DAILY_CAP per payee per local day of
        the mail. A held
        debit is recorded only on the user's explicit Yes (feedback)."""
        if money is None or money["direction"] != Direction.DEBIT.value:
            return False
        if decision.verdict is Verdict.ASK or not sender_established or lookalike:
            return False
        start = local_day_start(received, user.timezone)
        count = await repo.baselined_on(user.id, money["counterparty_key"], start, start + timedelta(days=1))
        return count < BASELINE_DAILY_CAP

    async def _money(
        self, user: Any, obs: Any, u: EmailUnderstanding, received: Any
    ) -> tuple[dict | None, AnomalyResult]:
        m = u.money
        if m is None or m.amount <= 0:
            return None, NO_ANOMALY
        s = get_settings()
        currency = m.currency or s.attention_currency
        known = await self._baselines.counterparty_keys(user.id, currency)
        key = match_key(normalize_counterparty(m.counterparty), known)
        occurred = timeutil.ensure_utc(m.occurred_at) if m.occurred_at else received
        hour = timeutil.to_local(occurred, user.timezone).hour
        money = {
            "amount": m.amount,
            "currency": currency,
            "direction": m.direction.value,
            "method": m.method.value,
            "counterparty_key": key,
            "occurred_at": occurred.isoformat(),
            "local_hour": hour,
        }
        if m.direction is not Direction.DEBIT:
            return money, NO_ANOMALY
        snapshot = await self._baselines.snapshot(user.id, currency, key, m.method.value)
        recent = await repo.recent_debits(user.id, received - BURST_WINDOW, exclude_id=obs.id)
        return money, score_money(
            MoneyContext(
                m.amount,
                m.direction,
                METHOD_LABELS[m.method],
                hour,
                snapshot,
                recent,
                s.attention_large_amounts.get(currency),
            )
        )

    async def _embedding_signals(
        self, user_id: int, summary: str
    ) -> tuple[list[float] | None, float, list[PrefHit]]:
        try:
            vector = await self._index.embed(summary)
            novelty = await self._index.novelty(user_id, vector)
            prefs = await self._index.prefs_near(user_id, vector, get_settings().attention_pref_similarity)
            return vector, novelty, prefs
        except Exception as exc:  # noqa: BLE001 - embeddings are an enhancement, never a dependency
            log.warning("attention.embedding_failed", error=type(exc).__name__)
            return None, 0.0, []

    async def _index_observation(
        self, user_id: int, obs_id: int, kind: str, vector: list[float] | None, received: Any
    ) -> str | None:
        if vector is None:
            return None
        try:
            return await self._index.add_observation(user_id, obs_id, kind, vector, received.isoformat())
        except Exception as exc:  # noqa: BLE001
            log.warning("attention.embedding_failed", error=type(exc).__name__)
            return None

    async def deliver_queued(self, user: Any, obs: Any, decision: AttentionDecision | None = None) -> str:
        decision = decision or AttentionDecision(
            Verdict(obs.verdict), obs.urgency, obs.score, tuple(obs.reasons)
        )
        if decision.verdict is Verdict.ASK and decision.urgency >= URGENT:
            local_day = timeutil.to_local(timeutil.now(), user.timezone).date().isoformat()
            async with self._thresholds.urgent_slot(user.id, local_day) as slot:
                if not slot.free:  # another urgency-5 message already went out today
                    decision = replace(decision, urgency=URGENT - 1)
                    await repo.set_fields(obs.id, urgency=decision.urgency)
                delivery = await self._speaker.speak(user, obs, decision)
                if delivery == SENT:
                    slot.claim()
        else:
            delivery = await self._speaker.speak(user, obs, decision)
        await repo.set_fields(obs.id, delivery=delivery)
        return delivery

    async def speak_deferred(self, user_id: int, reason: str) -> None:
        """system_attention_speak: retry a deferred ask/notify after revalidating it is still relevant."""
        try:
            obs = await repo.get(int(reason))
        except ValueError:
            return
        if obs is None or obs.user_id != user_id or obs.feedback is not None or obs.status != repo.DONE:
            return
        if obs.delivery not in (DEFERRED, repo.QUEUED):  # sent, dropped or expired: never again
            return
        if obs.verdict not in (Verdict.ASK.value, Verdict.NOTIFY.value):
            return
        if timeutil.now() - timeutil.ensure_utc(obs.received_at) > DEFERRED_STALE:
            await repo.set_fields(obs.id, delivery="expired")  # still in the brief and the evening wrap
            log.info("attention.deferred_stale", obs_id=obs.id)
            return
        await self.deliver_queued(await users.get(user_id), obs)

    async def enrich(self, event: Event) -> str:
        """ENRICHERS hook for emails forwarded to the reasoner: computed sender facts only (trusted)."""
        if event.type is not EventType.EMAIL_RECEIVED:
            return ""
        stats = await self._baselines.sender(event.user_id, str(event.payload.get("from_address", "")))
        established = "yes" if stats.established(timeutil.now()) else "no"
        return f"Attention facts: emails_seen_from_sender={stats.count}; established_sender={established}"
