"""Task board and artefacts data access."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select, update

from mavis.domain.tasks import TaskKind, TaskOrigin, TaskStatus
from mavis.store.db import Session, utcnow
from mavis.store.models import Artifact, Task

ACTIVE_STATUSES = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL)
_TERMINAL = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)


async def create(
    user_id: int,
    goal: str,
    context: str = "",
    kind: TaskKind = TaskKind.TASK,
    origin: TaskOrigin = TaskOrigin.USER,
    notify_on_complete: bool = True,
    parent_id: int | None = None,
    tainted: bool = False,
) -> int:
    async with Session() as s:
        t = Task(
            user_id=user_id, goal=goal, context=context, kind=kind.value, origin=origin.value,
            notify_on_complete=notify_on_complete, parent_id=parent_id, status=TaskStatus.QUEUED.value,
            tainted=tainted,
        )
        s.add(t)
        await s.commit()
        return t.id


async def get(task_id: int) -> Task | None:
    async with Session() as s:
        return await s.get(Task, task_id)


async def set_status(task_id: int, status: TaskStatus, **fields) -> None:
    values = {"status": status.value, **fields}
    if status is TaskStatus.RUNNING:
        values.setdefault("started_at", utcnow())
    if status in _TERMINAL:
        values.setdefault("finished_at", utcnow())
    async with Session() as s:
        await s.execute(update(Task).where(Task.id == task_id).values(**values))
        await s.commit()


async def claim(
    task_id: int,
    from_status: TaskStatus | Iterable[TaskStatus],
    to_status: TaskStatus,
    **fields,
) -> bool:
    """Atomic compare-and-set on status. True only for the caller that won.

    `from_status` may be one status or several. Extra `fields` (result_text, error, ...) are written in
    the same UPDATE, so a terminal transition can never overwrite a concurrent CANCELLED.
    """
    froms = [from_status] if isinstance(from_status, TaskStatus) else list(from_status)
    values: dict = {"status": to_status.value, **fields}
    if to_status is TaskStatus.RUNNING:
        values.setdefault("started_at", utcnow())
    if to_status in _TERMINAL:
        values.setdefault("finished_at", utcnow())
    async with Session() as s:
        res = await s.execute(
            update(Task).where(Task.id == task_id, Task.status.in_([f.value for f in froms])).values(**values)
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def save_plan(task_id: int, plan: dict) -> bool:
    """Store the plan unless the task already reached a terminal status."""
    async with Session() as s:
        res = await s.execute(
            update(Task)
            .where(Task.id == task_id, Task.status.not_in([x.value for x in _TERMINAL]))
            .values(plan=plan)
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def running_count(user_id: int) -> int:
    async with Session() as s:
        n = await s.scalar(
            select(func.count(Task.id)).where(
                Task.user_id == user_id, Task.status == TaskStatus.RUNNING.value
            )
        )
        return int(n or 0)


async def stale_running(user_id: int, started_before: datetime) -> list[Task]:
    """RUNNING tasks whose current run began before `started_before` (a run that never finished)."""
    async with Session() as s:
        rows = await s.scalars(
            select(Task).where(Task.user_id == user_id, Task.status == TaskStatus.RUNNING.value,
                               Task.started_at < started_before).order_by(Task.id)
        )
        return list(rows)


async def next_queued(user_id: int) -> Task | None:
    async with Session() as s:
        return await s.scalar(
            select(Task).where(Task.user_id == user_id, Task.status == TaskStatus.QUEUED.value)
            .order_by(Task.id).limit(1)
        )


async def active_for_user(user_id: int) -> list[Task]:
    async with Session() as s:
        rows = await s.scalars(
            select(Task).where(Task.user_id == user_id, Task.status.in_([x.value for x in ACTIVE_STATUSES]))
            .order_by(Task.id)
        )
        return list(rows)


async def cancel(user_id: int, task_id: int) -> bool:
    async with Session() as s:
        res = await s.execute(
            update(Task)
            .where(Task.id == task_id, Task.user_id == user_id,
                   Task.status.in_([x.value for x in ACTIVE_STATUSES]))
            .values(status=TaskStatus.CANCELLED.value, finished_at=utcnow())
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def add_artifact(task_id: int, user_id: int, kind: str, path: str, mime: str, *,
                       title: str = "", size: int = 0) -> int:
    async with Session() as s:
        a = Artifact(
            task_id=task_id, user_id=user_id, kind=kind, path=path, mime=mime, title=title, size=size
        )
        s.add(a)
        await s.commit()
        return a.id


async def artifacts_for(task_id: int) -> list[Artifact]:
    async with Session() as s:
        rows = await s.scalars(select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.id))
        return list(rows)
