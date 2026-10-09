"""EMAIL_RECEIVED router, the per-user LLM budget, the self-rescheduling drain and the first-sync backfill.

Registered with replace=True for EMAIL_RECEIVED (spec attention section 3.3). Promotional labels are logged
without an LLM; watched-loop matches go to the existing reasoner; everything else is understood under a
budget of ATTENTION_UNDERSTAND_PER_WINDOW calls per ATTENTION_WINDOW_S, behind chat."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.exc import NoResultFound

from mavis.attention.learning import Thresholds
from mavis.attention.pipeline import DEFERRED_STALE, AttentionPipeline
from mavis.attention.sanitize import clean, sender_domain, summary_text
from mavis.attention.scheduling import schedule_once
from mavis.attention.schema import EmailKind, Verdict
from mavis.attention.understand import BODY_LIMIT, heuristic
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.errors import LLMError
from mavis.domain.events import Event
from mavis.domain.integrations import UserRef
from mavis.domain.policy import Capability
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.email_triage import DROP_LABELS, classify
from mavis.initiative.filters import watch_matches
from mavis.llm import models as llm
from mavis.loops.service import LoopService
from mavis.store.repo import attention as repo
from mavis.store.repo import messages, users
from mavis.timers.service import WakeupService
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.normalize import email_event, extract_messages, to_datetime

log = structlog.get_logger()


def _reraise_if_strict(exc: Exception) -> None:
    """Tests run strict: a failure from a fake (FakeLLM "unexpected call") must surface, not be logged."""
    if get_settings().attention_strict_errors:
        raise exc


PENDING_KEYS = (
    "from",
    "from_address",
    "from_name",
    "subject",
    "snippet",
    "labels",
    "list_unsubscribe",
    "received_at",
    "sender_authenticated",  # a bool computed by normalize; the raw header is never stored
)
CHAT_YIELD = timedelta(seconds=20)
REDELIVER_AFTER = timedelta(seconds=60)
BACKFILL_DAYS = 14
BACKFILL_TEMPLATE = "newer_than:{days}d -in:sent -category:promotions -category:social"
BACKFILL_QUERY = BACKFILL_TEMPLATE.format(days=BACKFILL_DAYS)
DRAIN_REASON, BACKFILL_REASON = "drain", "backfill"
DEAD_LABELS = frozenset({"SPAM", "TRASH"})
DRAIN_BACKOFF_MAX = timedelta(minutes=15)
REDELIVER_MAX = 3  # re-send attempts for a queued ping before it is given up (it stays in the brief)
BACKFILL_PAGE = 50  # the mail.search maximum
EXPIRED = "expired"


def label_dropped(p: dict) -> bool:
    """Spam and trash always; promotional, social and forums mail unless it looks like a security notice
    (Gmail files many sign-in and password alerts under Social). Such mail is understood, but its
    facts["bulk"] marker still denies it the security budget bypass."""
    labels = set(p.get("labels") or [])
    if labels & DEAD_LABELS:
        return True
    return bool(labels & DROP_LABELS) and "security" not in classify(p)


Forward = Callable[[Event], Awaitable[None]]
OnEmpty = Callable[[Any], Awaitable[object]]


class Intake:
    def __init__(
        self,
        *,
        pipeline: AttentionPipeline,
        loops: LoopService,
        wakeups: WakeupService,
        thresholds: Thresholds,
        forward: Forward,
        provider: IntegrationProvider | None = None,
        on_backlog_empty: OnEmpty | None = None,
        connectors: Any = None,
    ) -> None:
        self._connectors = connectors  # attention.connector_ingest.ConnectorIngest: mail into the graph
        self._pipeline, self._loops, self._wakeups, self._thresholds = pipeline, loops, wakeups, thresholds
        self._forward, self._provider, self._on_backlog_empty = forward, provider, on_backlog_empty

    async def on_email(self, event: Event) -> None:
        p = event.payload
        if self._connectors is not None:
            await self._connectors.email(event.user_id, p)  # never raises; bulk and own mail stay out
        labels = set(p.get("labels") or [])
        if p.get("from_me") or "SENT" in labels or not p.get("message_id"):
            return
        try:
            user = await users.get(event.user_id)
        except NoResultFound:
            return
        obs, created = await self.ingest(user.id, p, repo.ORIGIN_LIVE)
        try:
            await self._route(user, event, obs, created)
        except LLMError as exc:  # composing a notify failed: the row stays queued, the drain retries it
            log.warning("attention.speak_failed", obs_id=obs.id, error=type(exc).__name__)
            await self._ensure_drain(user.id, at=self._backoff_at(failed=True))

    async def _route(self, user: Any, event: Event, obs: Any, created: bool) -> None:
        p = event.payload
        if not created and obs.status == repo.DONE:
            if obs.delivery == repo.QUEUED:  # a crash between deciding and speaking: finish the job
                await self._pipeline.deliver_queued(user, obs)
            return
        if not created and obs.origin == repo.ORIGIN_BACKFILL:
            # backfill saw it first, around connect time: the live copy may still speak if it is fresh
            await repo.set_fields(obs.id, origin=repo.ORIGIN_LIVE)
            obs.origin = repo.ORIGIN_LIVE
        if label_dropped(p):
            await self._pipeline.finalize_cheap(user, obs, p, Verdict.DROPPED)
            return
        if any(watch_matches(lp, event) for lp in await self._loops.active(user.id)):
            await self._forward(event)  # first: a retry forwards again and the reasoner dedupes by event id
            await self._pipeline.finalize_cheap(user, obs, p, Verdict.FORWARDED)
            return
        if await self._may_understand(user.id) and await self._process_safely(user, obs):
            return
        await self._ensure_drain(user.id)

    async def _process_safely(self, user: Any, obs: Any) -> bool:
        """process(), but an unexpected error in one row never escapes to the bus (redelivery would only
        repeat the LLM call and crash again). LLMError still propagates: it means a compose failed."""
        try:
            return await self._pipeline.process(user, obs)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad row must never block the queue
            _reraise_if_strict(exc)
            await self._row_failed(obs.id, exc)
            return False

    async def _row_failed(self, obs_id: int, exc: Exception) -> None:
        """Log it; once the row has used its attempts, close it so the queue moves on. Mail that cannot be
        read is still run through the keyword classifier, so a fraud or security notice is never dropped."""
        log.warning("attention.process_failed", obs_id=obs_id, error=type(exc).__name__, exc_info=exc)
        fresh = await repo.get(obs_id)
        if fresh is None or fresh.status != repo.PENDING:
            return
        if fresh.attempts >= get_settings().attention_max_attempts:
            await self._close_unreadable(fresh)

    async def _close_unreadable(self, obs: Any) -> None:
        payload = dict(obs.pending_payload or {})
        try:
            u = heuristic(payload)
        except Exception:  # noqa: BLE001 - the classifier must not stop the close
            log.warning("attention.heuristic_failed", obs_id=obs.id, exc_info=True)
            u = None
        risky = u is not None and u.kind in (EmailKind.SECURITY, EmailKind.MONEY_MOVEMENT)
        if u is not None:
            try:  # the normal decision path with the heuristic cap (never ask, urgency at most 4)
                user = await users.get(obs.user_id)
                await self._pipeline.finalize(user, obs, payload, u, "heuristic")
                await self._lift_to_brief(obs.id, risky)
                return
            except LLMError:
                await self._lift_to_brief(obs.id, risky)
                return  # decided and stored; only the notify compose failed, redelivery retries it
            except Exception:  # noqa: BLE001 - finalize is what failed before; close by hand
                log.warning("attention.heuristic_finalize_failed", obs_id=obs.id, exc_info=True)
        kind = u.kind if u is not None else EmailKind.OTHER
        # risky mail waits in the morning brief as an untrusted line instead of vanishing as routine
        await repo.finish(
            obs.id,
            method="error",
            kind=kind.value,
            verdict="brief" if risky else "log",
            urgency=0,
            summary=summary_text(kind, obs.sender_domain, str(payload.get("subject", "")))
            if risky
            else "(could not be read)",
        )

    @staticmethod
    async def _lift_to_brief(obs_id: int, risky: bool) -> None:
        """Security or money mail that the keyword pass left as routine still reaches the morning brief."""
        fresh = await repo.get(obs_id)
        if risky and fresh is not None and fresh.verdict in ("log", "dropped"):
            await repo.set_fields(obs_id, verdict="brief")

    async def ingest(self, user_id: int, p: dict, origin: str) -> tuple[Any, bool]:
        payload = {k: p.get(k) for k in PENDING_KEYS}
        payload["snippet"] = str(payload.get("snippet") or "")[:BODY_LIMIT]
        address = str(p.get("from_address", "")).lower()
        return await repo.insert_pending(
            user_id,
            str(p["message_id"])[:200],
            thread_id=str(p.get("thread_id") or "")[:200],
            origin=origin,
            sender_domain=sender_domain(address)[:120],
            sender_name=clean(p.get("from_name", ""), 80),
            received_at=to_datetime(p.get("received_at")) or timeutil.now(),
            payload=payload,
        )

    async def _may_understand(self, user_id: int) -> bool:
        if llm.unavailable_s() > 0:
            return False
        s = get_settings()
        since = timeutil.now() - timedelta(seconds=s.attention_window_s)
        return await repo.attempts_since(user_id, since) < s.attention_understand_per_window

    async def _user_active(self, user_id: int) -> bool:
        last = await messages.last_user_message_at(user_id)
        return last is not None and timedelta(0) <= timeutil.now() - last < CHAT_YIELD

    async def _ensure_drain(self, user_id: int, at: datetime | None = None) -> None:
        when = at or self._backoff_at(failed=False)
        await schedule_once(self._wakeups, user_id, WakeupKind.SYSTEM_ATTENTION_DRAIN, DRAIN_REASON, when)

    @staticmethod
    def _backoff_at(*, failed: bool) -> datetime:
        """Next drain: one budget window, or longer while the LLM is backing off or a send just failed."""
        delay = timedelta(seconds=get_settings().attention_window_s)
        if failed or llm.unavailable_s() > 0:
            delay = min(DRAIN_BACKOFF_MAX, max(2 * delay, timedelta(seconds=llm.unavailable_s())))
        return timeutil.now() + delay

    async def _redeliver(self, user: Any) -> bool:
        """Re-send queued pings. False when a compose failed: the row stays queued for the next drain."""
        if llm.unavailable_s() > 0:  # a notify needs the composer: do not spend the attempt now
            return True
        now = timeutil.now()
        for obs in await repo.undelivered(user.id, before=now - REDELIVER_AFTER):
            facts = dict(obs.facts or {})
            tries = int(facts.get("resends") or 0)
            if tries >= REDELIVER_MAX or now - timeutil.ensure_utc(obs.received_at) > DEFERRED_STALE:
                await repo.set_fields(obs.id, delivery=EXPIRED)  # still in the brief and the evening wrap
                log.info("attention.redeliver_given_up", obs_id=obs.id, tries=tries)
                continue
            facts["resends"] = tries + 1
            await repo.set_fields(obs.id, facts=facts)
            try:
                await self._pipeline.deliver_queued(user, obs)
            except LLMError as exc:
                log.warning("attention.redeliver_failed", obs_id=obs.id, error=type(exc).__name__)
                return False
            except Exception as exc:  # noqa: BLE001 - one bad row must not block the others
                _reraise_if_strict(exc)
                log.warning(
                    "attention.redeliver_failed", obs_id=obs.id, error=type(exc).__name__, exc_info=True
                )
        return True

    async def drain(self, user_id: int, reason: str = "") -> int:
        """system_attention_drain: redeliver queued pings, then understand pending mail within budget.
        A failing send never stalls the backlog, and the drain re-arms itself while work remains."""
        try:
            user = await users.get(user_id)
        except NoResultFound:
            return 0
        processed, failed, remaining = 0, False, 0
        skip: set[int] = set()  # rows that raised this round: the rest of the queue still moves
        try:
            failed = not await self._redeliver(user)
            while await self._may_understand(user_id) and not await self._user_active(user_id):
                batch = [o for o in await repo.pending(user_id, limit=len(skip) + 1) if o.id not in skip]
                if not batch:
                    break
                obs = batch[0]
                try:
                    ok = await self._pipeline.process(user, obs)
                except LLMError as exc:  # decided and stored, but the notify compose failed: still queued
                    log.warning("attention.speak_failed", obs_id=obs.id, error=type(exc).__name__)
                    failed = True
                    break
                except Exception as exc:  # noqa: BLE001 - one bad row must never block the queue
                    _reraise_if_strict(exc)
                    await self._row_failed(obs.id, exc)
                    skip.add(obs.id)
                    continue
                if not ok:
                    break
                processed += 1
        finally:
            remaining = await repo.pending_count(user_id)
            queued = bool(await repo.undelivered(user_id, before=timeutil.now()))
            if remaining or queued:
                await self._ensure_drain(user_id, at=self._backoff_at(failed=failed))
        if not remaining and self._on_backlog_empty is not None:
            await self._on_backlog_empty(user)
        log.info("attention.drain", user_id=user_id, processed=processed, remaining=remaining)
        return processed

    async def start_backfill(self, user_id: int) -> None:
        await schedule_once(
            self._wakeups, user_id, WakeupKind.SYSTEM_ATTENTION_BACKFILL, BACKFILL_REASON, timeutil.now()
        )

    async def on_task_completed(self, event: Event) -> None:
        """Appended after Phase 5's TASK_COMPLETED dispatcher: a finished Gmail first sync starts warm-up."""
        p = event.payload
        if p.get("kind") == "first_sync" and p.get("capability") == Capability.GMAIL.value:
            await self.start_backfill(event.user_id)

    async def backfill(self, user_id: int, reason: str = "") -> int:
        """system_attn_backfill: queue the last 14 days as backfill observations (baselines, never pings)."""
        if self._provider is None or (await self._thresholds.load(user_id)).get("backfilled_at"):
            return 0
        try:
            user = await users.get(user_id)
        except NoResultFound:
            return 0
        s = get_settings()
        # never reach past retention: a purged observation must not be re-created from old mail
        days = max(1, min(BACKFILL_DAYS, s.attention_retention_days))
        cutoff = timeutil.now() - timedelta(days=days)
        total, fetched, created = max(1, s.attention_backfill_max), 0, 0
        before: int | None = None
        # page backwards by time until `days` or `total` messages, whichever comes first; queueing costs no
        # LLM call (the drain understands them within the per-poll budget)
        for _ in range(-(-total // BACKFILL_PAGE)):
            want = min(BACKFILL_PAGE, total - fetched)
            query = BACKFILL_TEMPLATE.format(days=days) + (f" before:{before}" if before else "")
            res = await self._provider.execute(
                UserRef(user_id=user_id), "mail.search", {"query": query, "max_results": want}
            )
            if not res.ok:
                log.warning("attention.backfill_failed", user_id=user_id, error=str(res.error)[:120])
                if fetched == 0:
                    return 0  # nothing queued: backfilled_at stays unset and heal retries
                break
            raws = extract_messages(res.data)
            fetched += len(raws)
            oldest: datetime | None = None
            for raw in raws:
                event = email_event(user_id, raw, source="backfill")
                if event is None:
                    continue
                received = to_datetime(event.payload.get("received_at"))
                if received is not None:
                    oldest = received if oldest is None else min(oldest, received)
                if event.payload.get("from_me") or (received is not None and received < cutoff):
                    continue
                obs, new = await self.ingest(user_id, event.payload, repo.ORIGIN_BACKFILL)
                if not new:
                    continue
                created += 1
                if label_dropped(event.payload):
                    await self._pipeline.finalize_cheap(user, obs, event.payload, Verdict.DROPPED)
            if len(raws) < want or fetched >= total or oldest is None or oldest < cutoff:
                break
            stamp = int(oldest.timestamp())
            if before is not None and stamp >= before:
                break  # no progress: the provider ignored the bound
            before = stamp
        await self._thresholds.patch(user_id, backfilled_at=timeutil.now().isoformat())
        await self._ensure_drain(user_id, at=timeutil.now())
        log.info("attention.backfill", user_id=user_id, queued=created)
        return created

    async def heal(self, user_id: int) -> None:
        """Startup and morning: re-arm a drain for leftover work and start a missing Gmail backfill."""
        if await repo.pending_count(user_id) or await repo.undelivered(user_id, before=timeutil.now()):
            await self._ensure_drain(user_id, at=timeutil.now())
        polling = (await users.get_state(user_id)).get("polling") or {}
        if polling.get(Capability.GMAIL.value) and not (await self._thresholds.load(user_id)).get(
            "backfilled_at"
        ):
            await self.start_backfill(user_id)

    async def heal_all(self) -> None:
        for user_id in await users.all_ids():
            try:
                await self.heal(user_id)
            except Exception as exc:  # noqa: BLE001 - one user must not block the rest
                log.warning("attention.heal_failed", user_id=user_id, error=type(exc).__name__)
