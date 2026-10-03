"""Worker job handlers for memory: LEARN (after each turn/signal) and CONSOLIDATE (nightly).

Both run off the reply path, serialised per user under a lock distinct from the turn lock.

Both are best effort: an LLMError (deadline, slot wait, timeout, 429 backoff, invalid output) is
logged once and the job is acked. Retrying it inline or by redelivery only holds the single LLM slot
(timeout plus cooldown per attempt) while chat turns and approved actions wait behind it.
Non-LLM failures (graph, vector store, database) still raise so the bus retries them.
"""

from __future__ import annotations

import structlog

from mavis.domain.errors import LLMError
from mavis.domain.events import Job, JobKind, Trust
from mavis.memory.consolidate import consolidate
from mavis.memory.service import get_memory
from mavis.memory.summaries import maybe_summarize
from mavis.store.repo import events
from mavis.worker.locks import lock
from mavis.worker.runner import register_job_handler

log = structlog.get_logger(__name__)


def learn_lock(user_id: int):
    return lock(f"learn:{user_id}")


async def handle_learn(job: Job) -> None:
    p = job.payload
    source_ref = str(p.get("source_ref", ""))
    marker = f"learn:{source_ref}"
    async with learn_lock(job.user_id):
        # Redelivery guard: a re-run would extract again (differently) and re-fire hooks.
        if source_ref and await events.seen(marker):
            return
        try:
            await get_memory().learn(job.user_id, str(p.get("text", "")), source_ref,
                                     Trust(p.get("trust", Trust.USER.value)))
        except LLMError as exc:
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
