"""Task board and artefacts data access."""

from __future__ import annotations

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
) -> int:
    async with Session() as s:
        t = Task(
            user_id=user_id, goal=goal, context=context, kind=kind.value, origin=origin.value,
            notify_on_complete=notify_on_complete, parent_id=parent_id, status=TaskStatus.QUEUED.value,
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


async def claim(task_id: int, from_status: TaskStatus, to_status: TaskStatus) -> bool:
    """Atomic compare-and-set on status. True only for the caller that won."""
    values: dict = {"status": to_status.value}
    if to_status is TaskStatus.RUNNING:
        values["started_at"] = utcnow()
    async with Session() as s:
        res = await s.execute(
            update(Task).where(Task.id == task_id, Task.status == from_status.value).values(**values)
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
