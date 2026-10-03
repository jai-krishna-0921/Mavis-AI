"""Create task rows and enqueue RUN_TASK jobs (used by chat turns and the initiative agent)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from mavis import bus as bus_mod
from mavis.domain.decisions import TaskRequest
from mavis.domain.events import Job, JobKind
from mavis.domain.tasks import TaskOrigin
from mavis.store.repo import tasks


async def enqueue_run(task_id: int, user_id: int, bus: Any = None) -> None:
    await (bus or bus_mod.get_bus()).enqueue(Job(
        id=f"task:{task_id}:run:{uuid4().hex[:8]}", user_id=user_id, kind=JobKind.RUN_TASK,
        payload={"task_id": task_id},
    ))


async def dispatch_task_requests(
    user_id: int, requests: list[TaskRequest], origin: TaskOrigin, bus: Any = None, *,
    tainted: bool = False, source_ref: str | None = None,
) -> list[int]:
    """`tainted`: the requester saw third-party content; every step of these tasks runs tainted.
    `source_ref`: idempotency key (a retried caller gets the same task back; RUN_TASK is re-enqueued,
    which is harmless: run_task only claims a QUEUED task once)."""
    ids: list[int] = []
    for i, req in enumerate(requests):
        ref = source_ref if source_ref is None or len(requests) == 1 else f"{source_ref}:{i}"
        task_id = await tasks.create(
            user_id, goal=req.goal, context=req.context, origin=origin,
            notify_on_complete=req.notify_on_complete, tainted=tainted, source_ref=ref,
        )
        await enqueue_run(task_id, user_id, bus)
        ids.append(task_id)
    return ids
