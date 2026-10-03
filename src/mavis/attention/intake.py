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
from mavis.attention.pipeline import AttentionPipeline
from mavis.attention.sanitize import clean, sender_domain
from mavis.attention.scheduling import schedule_once
from mavis.attention.schema import Verdict
from mavis.attention.understand import BODY_LIMIT
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.events import Event
from mavis.domain.integrations import UserRef
from mavis.domain.policy import Capability
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.email_triage import DROP_LABELS
from mavis.initiative.filters import watch_matches
from mavis.llm import models as llm
from mavis.loops.service import LoopService
from mavis.store.repo import attention as repo
from mavis.store.repo import messages, users
from mavis.timers.service import WakeupService
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.normalize import email_event, extract_messages, to_datetime

log = structlog.get_logger()

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
    ) -> None:
        self._pipeline, self._loops, self._wakeups, self._thresholds = pipeline, loops, wakeups, thresholds
        self._forward, self._provider, self._on_backlog_empty = forward, provider, on_backlog_empty

    async def on_email(self, event: Event) -> None:
        p = event.payload
        labels = set(p.get("labels") or [])
        if p.get("from_me") or "SENT" in labels or not p.get("message_id"):
            return
        try:
            user = await users.get(event.user_id)
        except NoResultFound:
            return
        obs, created = await self.ingest(user.id, p, repo.ORIGIN_LIVE)
        if not created and obs.status == repo.DONE:
            if obs.delivery == repo.QUEUED:  # a crash between deciding and speaking: finish the job
                await self._pipeline.deliver_queued(user, obs)
            return
        if labels & DROP_LABELS:
            await self._pipeline.finalize_cheap(user, obs, p, Verdict.DROPPED)
            return
        if any(watch_matches(lp, event) for lp in await self._loops.active(user.id)):
            await self._forward(event)  # first: a retry forwards again and the reasoner dedupes by event id
            await self._pipeline.finalize_cheap(user, obs, p, Verdict.FORWARDED)
            return
        if await self._may_understand(user.id) and await self._pipeline.process(user, obs):
            return
        await self._ensure_drain(user.id)

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
        when = at or timeutil.now() + timedelta(seconds=get_settings().attention_window_s)
        await schedule_once(self._wakeups, user_id, WakeupKind.SYSTEM_ATTENTION_DRAIN, DRAIN_REASON, when)

    async def drain(self, user_id: int, reason: str = "") -> int:
        """system_attention_drain: redeliver queued pings, then understand pending mail within budget."""
        try:
            user = await users.get(user_id)
        except NoResultFound:
            return 0
        for obs in await repo.undelivered(user_id, before=timeutil.now() - REDELIVER_AFTER):
            await self._pipeline.deliver_queued(user, obs)
        processed = 0
        while await self._may_understand(user_id) and not await self._user_active(user_id):
            batch = await repo.pending(user_id, limit=1)
            if not batch or not await self._pipeline.process(user, batch[0]):
                break
            processed += 1
        remaining = await repo.pending_count(user_id)
        if remaining:
            await self._ensure_drain(user_id)
        elif self._on_backlog_empty is not None:
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
        res = await self._provider.execute(
            UserRef(user_id=user_id),
            "mail.search",
            {"query": BACKFILL_TEMPLATE.format(days=days), "max_results": s.attention_backfill_max},
        )
        if not res.ok:
            log.warning("attention.backfill_failed", user_id=user_id, error=str(res.error)[:120])
            return 0
        created = 0
        for raw in extract_messages(res.data):
            event = email_event(user_id, raw, source="backfill")
            if event is None or event.payload.get("from_me"):
                continue
            received = to_datetime(event.payload.get("received_at"))
            if received is not None and received < cutoff:
                continue
            obs, new = await self.ingest(user_id, event.payload, repo.ORIGIN_BACKFILL)
            if not new:
                continue
            created += 1
            if set(event.payload.get("labels") or []) & DROP_LABELS:
                await self._pipeline.finalize_cheap(user, obs, event.payload, Verdict.DROPPED)
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
