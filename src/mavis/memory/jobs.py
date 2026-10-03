"""Worker job handlers for memory: LEARN (after each turn/signal) and CONSOLIDATE (nightly).

Both run off the reply path, serialised per user under a lock distinct from the turn lock.

Both are best effort: an LLMError (deadline, slot wait, timeout, 429 backoff, invalid output) is
never retried inline or by redelivery, which only holds the single LLM slot (timeout plus cooldown per
attempt) while chat turns and approved actions wait behind it. A busy LEARN is re-tried later on a
system_learn wakeup (LEARN_RETRY_DELAYS, at most twice), then dropped; CONSOLIDATE is dropped (it runs
nightly). A LEARN job with a future `not_before` (chat turns: after the interactive grace window, so it
does not just lose to the reply's follow-up calls) is parked on a wakeup instead of run.
Non-LLM failures (graph, vector store, database) still raise so the bus retries them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from mavis.domain import timeutil
from mavis.domain.errors import LLMError
from mavis.domain.events import Job, JobKind, Trust
from mavis.domain.wakeups import WakeupKind
from mavis.memory.consolidate import consolidate
from mavis.memory.service import get_memory
from mavis.memory.summaries import maybe_summarize
from mavis.store.repo import events
from mavis.worker.locks import lock
from mavis.worker.runner import register_job_handler

log = structlog.get_logger(__name__)

LEARN_RETRY_DELAYS = (timedelta(minutes=3), timedelta(minutes=10))
_LEARN_REASON = "learn"


async def park_learn(user_id: int, payload: dict, at: datetime, tag: str) -> None:
    """Run this LEARN later: a system_learn wakeup re-enqueues the job at `at` (deduped per tag)."""
    from mavis.timers.service import WakeupService  # lazy: timers import the store, not memory

    source_ref = str(payload.get("source_ref", ""))
    await WakeupService().wake_me(
        user_id, at, _LEARN_REASON, kind=WakeupKind.SYSTEM_LEARN, payload={"learn": payload},
        dedupe_key=f"learn:{source_ref}:{tag}"[:150] if source_ref else None, scale=False,
    )


async def on_learn_wakeup(user_id: int, reason: str, payload: dict) -> None:
    from mavis.bus import get_bus  # lazy: the bus is set up after handlers register

    learn = dict(payload.get("learn") or {})
    if not learn:
        return
    retry = int(learn.get("retry", 0))
    await get_bus().enqueue(Job(
        id=f"learn:{learn.get('source_ref', '')}:w{retry}", user_id=user_id, kind=JobKind.LEARN,
        payload=learn,
    ))


def _not_before(p: dict) -> datetime | None:
    raw = p.get("not_before")
    try:
        return timeutil.ensure_utc(datetime.fromisoformat(str(raw))) if raw else None
    except ValueError:
        return None


def learn_lock(user_id: int):
    return lock(f"learn:{user_id}")


async def handle_learn(job: Job) -> None:
    p = job.payload
    source_ref = str(p.get("source_ref", ""))
    marker = f"learn:{source_ref}"
    if (due := _not_before(p)) is not None and due > timeutil.now():
        await park_learn(job.user_id, p, due, "delay")
        return
    async with learn_lock(job.user_id):
        # Redelivery guard: a re-run would extract again (differently) and re-fire hooks.
        if source_ref and await events.seen(marker):
            return
        try:
            await get_memory().learn(job.user_id, str(p.get("text", "")), source_ref,
                                     Trust(p.get("trust", Trust.USER.value)))
        except LLMError as exc:
            retry = int(p.get("retry", 0))
            if retry < len(LEARN_RETRY_DELAYS):
                at = timeutil.now() + LEARN_RETRY_DELAYS[retry]
                await park_learn(job.user_id, {**p, "retry": retry + 1, "not_before": None}, at,
                                 f"r{retry + 1}")
                log.warning("memory.learn_deferred_llm_busy", job_id=job.id, source_ref=source_ref,
                            retry=retry + 1, error=str(exc))
            else:
                log.warning("memory.learn_dropped_llm_busy", job_id=job.id, source_ref=source_ref,
                            error=str(exc))
            return
        if p.get("conversation", True):
            await maybe_summarize(job.user_id)
        if source_ref:
            await events.record(marker)


async def handle_consolidate(job: Job) -> None:
    async with learn_lock(job.user_id):
        try:
            await consolidate(job.user_id, get_memory())
        except LLMError as exc:
            log.warning("memory.consolidate_dropped_llm_busy", job_id=job.id, error=str(exc))


def register() -> None:
    register_job_handler(JobKind.LEARN, handle_learn)
    register_job_handler(JobKind.CONSOLIDATE, handle_consolidate)
    from mavis.timers.system import register_system_wakeup  # lazy: keep import order unchanged

    register_system_wakeup(WakeupKind.SYSTEM_LEARN.value, on_learn_wakeup, with_payload=True)
