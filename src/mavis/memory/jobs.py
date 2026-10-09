"""Worker job handlers for memory: LEARN (after each turn/signal) and CONSOLIDATE (nightly).

Both run off the reply path, serialised per user under a lock distinct from the turn lock.

An LLMError (deadline, slot wait, timeout, 429 backoff, invalid output) is never retried inline or by
bus redelivery, which only holds an LLM slot (timeout plus cooldown per attempt) while chat turns wait
behind it. LEARN is the only way a user's facts, loops and profile get learned, so it is NOT best effort
in the sense of "may be dropped": a failed LEARN is parked on a durable system_learn wakeup and retried
(LEARN_RETRY_DELAYS, then every LEARN_RETRY_CAP) until it succeeds; only a job older than LEARN_MAX_AGE
is dropped, with an error log. CONSOLIDATE is dropped (it runs nightly). A LEARN job with a future
`not_before` is parked on a wakeup instead of run.
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
from mavis.store.repo import wakeups as wakeups_repo
from mavis.worker.locks import lock
from mavis.worker.runner import register_job_handler

log = structlog.get_logger(__name__)

LEARN_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=3), timedelta(minutes=10))
LEARN_RETRY_CAP = timedelta(minutes=15)  # steady cadence once the short delays are used up
LEARN_COALESCE_ABOVE = 6  # parked LEARN texts of one user beyond which they are run as one extraction
LEARN_BATCH_CHARS = 8000
LEARN_MAX_AGE = timedelta(hours=24)  # a LEARN older than this is stale (relative dates, context): dropped
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


def _anchor(p: dict) -> datetime | None:
    """When the learned text was written (a LEARN job may run long after)."""
    raw = p.get("anchor_at")
    try:
        return timeutil.ensure_utc(datetime.fromisoformat(str(raw))) if raw else None
    except ValueError:
        return None


def learn_lock(user_id: int):
    return lock(f"learn:{user_id}")


async def _handle_record(job: Job) -> None:
    """A Gmail or Slack record (memory.records): learned on its own, per user, never batched with other
    texts, so its facts keep the record they came from. The same record twice is a no-op."""
    from mavis.memory import records

    p = job.payload
    source_ref = str(p.get("source_ref", ""))
    marker = records.record_marker(job.user_id, source_ref)
    async with learn_lock(job.user_id):
        if await events.seen(marker):
            return
        try:
            await records.learn_record(get_memory(), job.user_id, p)
        except LLMError as exc:
            await _defer_learn(job, p, exc)
            return
        await events.record(marker)


async def handle_learn(job: Job) -> None:
    p = job.payload
    if p.get("record"):
        await _handle_record(job)
        return
    source_ref = str(p.get("source_ref", ""))
    marker = f"learn:{source_ref}"
    if (due := _not_before(p)) is not None and due > timeutil.now():
        await park_learn(job.user_id, p, due, "delay")
        return
    async with learn_lock(job.user_id):
        # Redelivery guard: a re-run would extract again (differently) and re-fire hooks.
        if source_ref and await events.seen(marker):
            return
        p, consumed = await _coalesce(job.user_id, p)
        source_ref = str(p.get("source_ref", ""))
        try:
            await get_memory().learn(job.user_id, str(p.get("text", "")), source_ref,
                                     Trust(p.get("trust", Trust.USER.value)),
                                     conversation=bool(p.get("conversation", True)),
                                     anchor_at=_anchor(p), strict=bool(p.get("tainted", False)))
        except LLMError as exc:
            await _defer_learn(job, p, exc)
            await wakeups_repo.cancel_ids(consumed)  # the batch is parked again as one
            return
        if p.get("conversation", True):
            await maybe_summarize(job.user_id)
        for ref in {source_ref, *p.get("merged_refs", [])} - {""}:
            await events.record(f"learn:{ref}")
        await wakeups_repo.cancel_ids(consumed)


async def _coalesce(user_id: int, p: dict) -> tuple[dict, list[int]]:
    """Demand beyond the best_effort share of the slots (many users, a long outage) piles up as parked LEARN
    wakeups. Once more than LEARN_COALESCE_ABOVE of this user's, with the same trust and origin, wait, run
    them as ONE extraction: their texts in order, each under its own source time, applied no earlier than
    the newest statement. Returns the (possibly combined) payload and the parked wakeup ids it consumed."""
    from mavis.store.repo import users as users_repo  # lazy: the job layer starts before the store is used

    def group(x: dict) -> tuple:
        return (x.get("trust", Trust.USER.value), bool(x.get("conversation", True)), bool(x.get("tainted")),
                bool(x.get("record")))

    waiting = await wakeups_repo.list_pending(user_id, WakeupKind.SYSTEM_LEARN)
    parked = [(w.id, w.payload["learn"]) for w in waiting
              if isinstance(w.payload.get("learn"), dict) and group(w.payload["learn"]) == group(p)]
    if len(parked) < LEARN_COALESCE_ABOVE:
        return p, []
    now = timeutil.now()
    tz = (await users_repo.get(user_id)).timezone

    def when(x: dict) -> datetime:
        return _anchor(x) or now

    items = sorted([*parked, (None, p)], key=lambda it: when(it[1]))
    chosen, size = [], 0
    for wid, item in reversed(items):  # newest first: what does not fit stays parked for the next batch
        size += len(str(item.get("text", "")))
        if chosen and size > LEARN_BATCH_CHARS:
            break
        chosen.append((wid, item))
    chosen.reverse()
    text = "\n".join(
        f"[{timeutil.to_local(when(i), tz):%a %d %b %H:%M}]\n{i.get('text', '')}" for _, i in chosen)
    refs = [str(i.get("source_ref", "")) for _, i in chosen if i.get("source_ref")]
    firsts = [str(i["first_at"]) for _, i in chosen if i.get("first_at")]
    merged = {**p, "text": text, "anchor_at": max(when(i) for _, i in chosen).isoformat(),
              "merged_refs": refs, "retry": max(int(i.get("retry", 0)) for _, i in chosen),
              "not_before": None, **({"first_at": min(firsts)} if firsts else {})}
    log.info("memory.learn_coalesced", user_id=user_id, texts=len(chosen))
    return merged, [wid for wid, _ in chosen if wid is not None]


def _learn_retry_delay(retry: int) -> timedelta:
    return LEARN_RETRY_DELAYS[retry] if retry < len(LEARN_RETRY_DELAYS) else LEARN_RETRY_CAP


async def _defer_learn(job: Job, p: dict, exc: LLMError) -> None:
    """Park the job for another try, or drop it when it has been failing for LEARN_MAX_AGE."""
    now = timeutil.now()
    retry = int(p.get("retry", 0))
    source_ref = str(p.get("source_ref", ""))
    try:
        first = timeutil.ensure_utc(datetime.fromisoformat(str(p["first_at"])))
    except (KeyError, ValueError):
        first = now
    if now - first > LEARN_MAX_AGE:
        # Final: the facts AND any loops this text would have created are lost.
        log.error("memory.learn_dropped_final", job_id=job.id, source_ref=source_ref, attempts=retry + 1,
                  error=str(exc))
        return
    retried = {**p, "retry": retry + 1, "not_before": None, "first_at": first.isoformat()}
    await park_learn(job.user_id, retried, now + _learn_retry_delay(retry), f"r{retry + 1}")
    log.warning("memory.learn_deferred_llm_busy", job_id=job.id, source_ref=source_ref, retry=retry + 1,
                error=str(exc))


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
