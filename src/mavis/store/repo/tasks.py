"""Task board and artefacts data access."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

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
    source_ref: str | None = None,
) -> int:
    """`source_ref` (unique per user): when a task with it exists already, return that task's id."""
    if source_ref is not None and (existing := await by_source_ref(user_id, source_ref)) is not None:
        return existing
    async with Session() as s:
        t = Task(
            user_id=user_id, goal=goal, context=context, kind=kind.value, origin=origin.value,
            notify_on_complete=notify_on_complete, parent_id=parent_id, status=TaskStatus.QUEUED.value,
            tainted=tainted, source_ref=source_ref,
        )
        s.add(t)
        try:
            await s.commit()
        except IntegrityError:
            if source_ref is None:
                raise
            await s.rollback()  # lost a race with a concurrent create for the same source_ref
            found = await by_source_ref(user_id, source_ref)
            if found is None:
                raise
            return found
        return t.id


async def by_source_ref(user_id: int, source_ref: str) -> int | None:
    async with Session() as s:
        return await s.scalar(select(Task.id).where(Task.user_id == user_id, Task.source_ref == source_ref))


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
    """RUNNING planned tasks. APPROVAL tasks only show a prompt and wait, so they take no task slot."""
    async with Session() as s:
        n = await s.scalar(
            select(func.count(Task.id)).where(
                Task.user_id == user_id, Task.status == TaskStatus.RUNNING.value,
                Task.kind != TaskKind.APPROVAL.value,
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


async def running_started_before(started_before: datetime, user_id: int | None = None) -> list[Task]:
    """Every user's (or one user's) RUNNING tasks whose current run began before `started_before`."""
    q = select(Task).where(Task.status == TaskStatus.RUNNING.value, Task.started_at < started_before)
    if user_id is not None:
        q = q.where(Task.user_id == user_id)
    async with Session() as s:
        return list(await s.scalars(q.order_by(Task.id)))


async def users_with_queued(user_id: int | None = None) -> list[int]:
    q = select(Task.user_id).where(Task.status == TaskStatus.QUEUED.value).distinct()
    if user_id is not None:
        q = q.where(Task.user_id == user_id)
    async with Session() as s:
        return sorted(await s.scalars(q))


async def next_queued(user_id: int) -> Task | None:
    """The oldest QUEUED planned task (APPROVAL tasks never wait for a slot)."""
    async with Session() as s:
        return await s.scalar(
            select(Task).where(Task.user_id == user_id, Task.status == TaskStatus.QUEUED.value,
                               Task.kind != TaskKind.APPROVAL.value)
            .order_by(Task.id).limit(1)
        )


async def queued_approval_tasks(user_id: int) -> list[Task]:
    async with Session() as s:
        rows = await s.scalars(
            select(Task).where(Task.user_id == user_id, Task.status == TaskStatus.QUEUED.value,
                               Task.kind == TaskKind.APPROVAL.value).order_by(Task.id)
        )
        return list(rows)


async def active_for_user(user_id: int) -> list[Task]:
    async with Session() as s:
        rows = await s.scalars(
            select(Task).where(Task.user_id == user_id, Task.status.in_([x.value for x in ACTIVE_STATUSES]))
            .order_by(Task.id)
        )
        return list(rows)


GOAL_DUPLICATE_SIMILARITY = 0.8
_WEEKDAYS = frozenset("monday tuesday wednesday thursday friday saturday sunday "
                      "mon tue tues wed thu thur thurs fri sat sun".split())


_RELATIVE_DAYS = frozenset("today tonight tomorrow yesterday weekend".split())
_PERIOD_LEADS = frozenset("this next last".split())
_PERIODS = frozenset("week month year weekend".split())
_NUMBER_WORDS = frozenset(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty dozen couple".split())


def _goal_words(goal: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", goal.casefold()).split()


def _goal_tokens(goal: str) -> set[str]:
    """Lowercased words with punctuation removed. Digits, days and times stay (unlike loop titles)."""
    return set(_goal_words(goal))


def _relative_words(goal: str) -> set[str]:
    """Relative days ("today"), periods ("next week") and number words ("two") a goal names."""
    words = _goal_words(goal)
    out = {w for w in words if w in _RELATIVE_DAYS or w in _NUMBER_WORDS}
    out |= {f"{a} {b}" for a, b in zip(words, words[1:], strict=False)
            if a in _PERIOD_LEADS and b in _PERIODS}
    return out


def same_goal(a: str, b: str) -> bool:
    """Task goals are one request when their tokens match exactly or by Jaccard >= 0.8, and their
    numbers/times (any token with a digit) and weekdays are the same. No subset rule: "Book flights
    to Paris" is not "Book flights and hotels to Paris". Relative days, periods and number words must
    match when both goals name one. Unlike loop dedupe there is no due-time
    guard, so days and times must count here. Decided for the incident pair ("...for today at 3 PM
    IST and send an interview invite to x@y" vs "...for the 3 PM IST interview and send the invite to
    x@y"): Jaccard 0.8 with the same numbers, so one task."""
    ta, tb = _goal_tokens(a), _goal_tokens(b)
    if not ta or not tb:
        return " ".join(a.casefold().split()) == " ".join(b.casefold().split())
    if ta == tb:
        return True

    def anchors(t: set[str]) -> set[str]:
        return {w for w in t if any(c.isdigit() for c in w) or w in _WEEKDAYS}

    if anchors(ta) != anchors(tb):
        return False
    # When BOTH name a relative day, period or number word, those must match ("today" vs "tomorrow",
    # "two" vs "four", "this week" vs "next week"). Named on one side only (the incident pair's
    # "today"), the word does not split them.
    ra, rb = _relative_words(a), _relative_words(b)
    if ra and rb and ra != rb:
        return False
    return len(ta & tb) / len(ta | tb) >= GOAL_DUPLICATE_SIMILARITY


async def find_active_duplicate(user_id: int, goal: str) -> Task | None:
    """A non-terminal planned task with the same goal (same_goal), so one request never runs twice."""
    for task in await active_for_user(user_id):
        if task.kind == TaskKind.TASK.value and same_goal(task.goal, goal):
            return task
    return None


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
