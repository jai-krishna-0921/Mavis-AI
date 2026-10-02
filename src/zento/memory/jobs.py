"""Worker job handlers for memory: LEARN (after each turn/signal) and CONSOLIDATE (nightly).

Both run off the reply path, serialised per user under a lock distinct from the turn lock.
"""

from __future__ import annotations

from zento.domain.events import Job, JobKind, Trust
from zento.memory.consolidate import consolidate
from zento.memory.service import get_memory
from zento.memory.summaries import maybe_summarize
from zento.worker.locks import lock
from zento.worker.runner import register_job_handler


def learn_lock(user_id: int):
    return lock(f"learn:{user_id}")


async def handle_learn(job: Job) -> None:
    p = job.payload
    async with learn_lock(job.user_id):
        await get_memory().learn(job.user_id, str(p.get("text", "")), str(p.get("source_ref", "")),
                                 Trust(p.get("trust", Trust.USER.value)))
        if p.get("conversation", True):
            await maybe_summarize(job.user_id)


async def handle_consolidate(job: Job) -> None:
    async with learn_lock(job.user_id):
        await consolidate(job.user_id, get_memory())


def register() -> None:
    register_job_handler(JobKind.LEARN, handle_learn)
    register_job_handler(JobKind.CONSOLIDATE, handle_consolidate)
