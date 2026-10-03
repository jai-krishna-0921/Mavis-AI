"""Create task rows and enqueue RUN_TASK jobs (used by chat turns and the initiative agent)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import structlog

from mavis import bus as bus_mod
from mavis.domain.decisions import TaskRequest
from mavis.domain.events import Job, JobKind
from mavis.domain.tasks import TaskOrigin
from mavis.store.repo import tasks

log = structlog.get_logger(__name__)


async def enqueue_run(task_id: int, user_id: int, bus: Any = None) -> None:
    await (bus or bus_mod.get_bus()).enqueue(Job(
        id=f"task:{task_id}:run:{uuid4().hex[:8]}", user_id=user_id, kind=JobKind.RUN_TASK,
        payload={"task_id": task_id},
    ))


async def find_duplicate(user_id: int, goal: str, source_ref: str | None = None) -> int | None:
    """The id of an active task already doing `goal`, or None. A retried caller (same source_ref)
    is not a duplicate: tasks.create hands it its own task back."""
    if source_ref is not None and await tasks.by_source_ref(user_id, source_ref) is not None:
        return None
    task = await tasks.find_active_duplicate(user_id, goal)
    return task.id if task is not None else None


async def dispatch_task_requests(
    user_id: int, requests: list[TaskRequest], origin: TaskOrigin, bus: Any = None, *,
    tainted: bool = False, source_ref: str | None = None,
) -> list[int]:
    """`tainted`: the requester saw third-party content; every step of these tasks runs tainted.
    `source_ref`: idempotency key (a retried caller gets the same task back; RUN_TASK is re-enqueued,
    which is harmless: run_task only claims a QUEUED task once). A request whose goal matches an
    active task (find_duplicate) gets that task's id and starts nothing."""
    ids: list[int] = []
    for i, req in enumerate(requests):
        ref = source_ref if source_ref is None or len(requests) == 1 else f"{source_ref}:{i}"
        if (dup := await find_duplicate(user_id, req.goal, ref)) is not None:
            log.info("task.duplicate_skipped", existing_task_id=dup, origin=origin.value, goal=req.goal[:80])
            ids.append(dup)
            continue
        task_id = await tasks.create(
            user_id, goal=req.goal, context=req.context, origin=origin,
            notify_on_complete=req.notify_on_complete, tainted=tainted, source_ref=ref,
        )
        await enqueue_run(task_id, user_id, bus)
        ids.append(task_id)
    return ids
