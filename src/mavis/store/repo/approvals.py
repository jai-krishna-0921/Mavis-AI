"""Pending approvals for outward / spend / destructive tool calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from mavis.domain.tasks import (
    OPEN_APPROVAL_STATUSES,
    TERMINAL_APPROVAL_STATUSES,
    ApprovalStatus,
    TaskStatus,
)
from mavis.store.db import Session, utcnow
from mavis.store.models import PendingApproval, Task

_OPEN = [s.value for s in OPEN_APPROVAL_STATUSES]


async def create(
    user_id: int, task_id: int | None, tool: str, arguments: dict, preview: str, expires_at: datetime,
    *, tainted: bool = False,
) -> int:
    async with Session() as s:
        a = PendingApproval(
            user_id=user_id, task_id=task_id, tool=tool, arguments=arguments, preview=preview,
            expires_at=expires_at, status=ApprovalStatus.PENDING.value, tainted=tainted,
        )
        s.add(a)
        await s.commit()
        return a.id


def args_hash(arguments: dict) -> str:
    canon = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


_WAITING = [ApprovalStatus.PENDING.value, ApprovalStatus.AWAITING_EDIT.value]


def _canon(value: Any) -> Any:
    if isinstance(value, str):
        try:  # one instant written two ways ("...+05:30" / "...Z") is the same start time
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None and "T" in value:
            return parsed.astimezone(UTC).isoformat()
        return " ".join(value.split()).casefold()
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return sorted((_canon(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return value


def equivalence_key(arguments: dict, identity: Sequence[str] = ()) -> str:
    """Canonical identity of an action for duplicate detection: whitespace and case folded, lists
    sorted, ISO datetimes in UTC. `identity` is the tool's declared identifying arguments; when it is
    empty, or any of those arguments is empty in this call (an invite with no guests), the action is
    identified by all of its canonical arguments."""
    canon = _canon(arguments or {})
    if identity and all(field in canon for field in identity):
        canon = {field: canon[field] for field in identity}
    return json.dumps(canon, sort_keys=True, default=str, ensure_ascii=False)


def equivalent(a: dict, b: dict, identity: Sequence[str] = ()) -> bool:
    return equivalence_key(a, identity) == equivalence_key(b, identity)


async def waiting_equivalents(user_id: int, tool: str, arguments: dict, *, identity: Sequence[str] = (),
                              tainted: bool | None, exclude_id: int | None = None) -> list[PendingApproval]:
    """This user's approvals still waiting on them (PENDING / AWAITING_EDIT) for the same tool and the
    same action identity, in any task, oldest first. `tainted` (True/False) keeps only rows of that
    taint: a request from a clean run is never merged into a card a tainted run queued (its text may be
    attacker-shaped), nor the reverse. None matches either (closing a card is always safe)."""
    want = equivalence_key(arguments, identity)
    async with Session() as s:
        rows = await s.scalars(
            select(PendingApproval)
            .where(PendingApproval.user_id == user_id, PendingApproval.tool == tool,
                   PendingApproval.status.in_(_WAITING))
            .order_by(PendingApproval.id)
        )
        return [r for r in rows if r.id != exclude_id and (tainted is None or bool(r.tainted) == tainted)
                and equivalence_key(r.arguments or {}, identity) == want]


async def pending_rows(user_id: int | None = None) -> list[PendingApproval]:
    """PENDING approvals (waiting on a tap), oldest first, for one user or everyone."""
    stmt = select(PendingApproval).where(PendingApproval.status == ApprovalStatus.PENDING.value)
    if user_id is not None:
        stmt = stmt.where(PendingApproval.user_id == user_id)
    async with Session() as s:
        return list(await s.scalars(stmt.order_by(PendingApproval.id)))


async def find_open(user_id: int, task_id: int | None, tool: str, arguments: dict) -> PendingApproval | None:
    """An open approval for the same user, task, tool and canonical arguments, if any."""
    want = args_hash(arguments)
    task_clause = PendingApproval.task_id.is_(None) if task_id is None else PendingApproval.task_id == task_id
    async with Session() as s:
        rows = await s.scalars(
            select(PendingApproval)
            .where(
                PendingApproval.user_id == user_id,
                PendingApproval.tool == tool,
                task_clause,
                PendingApproval.status.in_(_OPEN),
            )
            .order_by(PendingApproval.id)
        )
        for row in rows:
            if args_hash(row.arguments or {}) == want:
                return row
    return None


async def get(approval_id: int) -> PendingApproval | None:
    async with Session() as s:
        return await s.get(PendingApproval, approval_id)


async def open_for_user(user_id: int) -> list[PendingApproval]:
    async with Session() as s:
        rows = await s.scalars(
            select(PendingApproval)
            .where(PendingApproval.user_id == user_id, PendingApproval.status.in_(_OPEN))
            .order_by(PendingApproval.id)
        )
        return list(rows)


async def next_open(task_id: int) -> PendingApproval | None:
    async with Session() as s:
        return await s.scalar(
            select(PendingApproval)
            .where(PendingApproval.task_id == task_id, PendingApproval.status.in_(_OPEN))
            .order_by(PendingApproval.id).limit(1)
        )


async def unattached_for_user(user_id: int) -> list[PendingApproval]:
    async with Session() as s:
        rows = await s.scalars(
            select(PendingApproval).where(
                PendingApproval.user_id == user_id,
                PendingApproval.task_id.is_(None),
                PendingApproval.status == ApprovalStatus.PENDING.value,
            ).order_by(PendingApproval.id)
        )
        return list(rows)


async def attach(approval_ids: Iterable[int], task_id: int) -> None:
    """Attach still-open approvals to a task (resolved ones stay where they are)."""
    ids = list(approval_ids)
    if not ids:
        return
    async with Session() as s:
        await s.execute(
            update(PendingApproval)
            .where(PendingApproval.id.in_(ids), PendingApproval.status.in_(_OPEN))
            .values(task_id=task_id)
        )
        await s.commit()


async def claim(approval_id: int, from_statuses: Iterable[ApprovalStatus], to: ApprovalStatus) -> bool:
    """Atomic status transition; exactly one concurrent caller gets True."""
    values: dict = {"status": to.value}
    if to == ApprovalStatus.RESOLVING:
        values["resolving_at"] = utcnow()
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(PendingApproval.id == approval_id,
                   PendingApproval.status.in_([x.value for x in from_statuses]))
            .values(**values)
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def mark_started(approval_id: int) -> bool:
    """Record that an approved tool is about to run. Only for a row the caller claimed as EXECUTED
    and not already started, so a replay can tell "claimed, never run" from "may have run"."""
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(PendingApproval.id == approval_id,
                   PendingApproval.status == ApprovalStatus.EXECUTED.value,
                   PendingApproval.started_at.is_(None))
            .values(started_at=utcnow())
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def set_status(
    approval_id: int,
    status: ApprovalStatus,
    result: str | None = None,
    *,
    from_statuses: Iterable[ApprovalStatus] = OPEN_APPROVAL_STATUSES,
) -> bool:
    """Move an approval to `status` only while it is in one of `from_statuses` (default: open).

    A resolved approval is never reopened or relabelled. The executor that claimed RESOLVING to
    EXECUTED records its outcome with `from_statuses={ApprovalStatus.EXECUTED}`.
    """
    values: dict = {"status": status.value}
    if result is not None:
        values["result"] = result
    if status in TERMINAL_APPROVAL_STATUSES:
        values["resolved_at"] = utcnow()
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(PendingApproval.id == approval_id,
                   PendingApproval.status.in_([x.value for x in from_statuses]))
            .values(**values)
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def update_args(approval_id: int, arguments: dict, preview: str) -> bool:
    """Replace the arguments and preview and go back to PENDING, only while the approval is open."""
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(PendingApproval.id == approval_id, PendingApproval.status.in_(_OPEN))
            .values(arguments=arguments, preview=preview, status=ApprovalStatus.PENDING.value)
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def mark_prompted(approval_id: int) -> bool:
    """True the first time only (used to schedule expiry wakeups exactly once)."""
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(PendingApproval.id == approval_id, PendingApproval.prompted_at.is_(None))
            .values(prompted_at=utcnow())
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def executed_for_task(task_id: int) -> list[PendingApproval]:
    """Approvals whose action ran (EXECUTED and finished), oldest first."""
    async with Session() as s:
        rows = await s.scalars(
            select(PendingApproval)
            .where(PendingApproval.task_id == task_id,
                   PendingApproval.status == ApprovalStatus.EXECUTED.value,
                   PendingApproval.resolved_at.is_not(None))
            .order_by(PendingApproval.id)
        )
        return list(rows)


async def fail_unstarted_for_task(task_id: int, note: str) -> int:
    """Close approvals that can never run now: RESOLVING (decision never applied) and EXECUTED rows
    that were claimed but never started. FAILED, with `note` as the result."""
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(
                PendingApproval.task_id == task_id,
                (PendingApproval.status == ApprovalStatus.RESOLVING.value)
                | ((PendingApproval.status == ApprovalStatus.EXECUTED.value)
                   & PendingApproval.started_at.is_(None)
                   & PendingApproval.resolved_at.is_(None)),
            )
            .values(status=ApprovalStatus.FAILED.value, result=note, resolved_at=utcnow())
        )
        await s.commit()
        return res.rowcount or 0


async def may_have_run_for_task(task_id: int) -> list[PendingApproval]:
    """EXECUTED and started but never finished: the action may or may not have gone through."""
    async with Session() as s:
        rows = await s.scalars(
            select(PendingApproval)
            .where(PendingApproval.task_id == task_id,
                   PendingApproval.status == ApprovalStatus.EXECUTED.value,
                   PendingApproval.started_at.is_not(None),
                   PendingApproval.resolved_at.is_(None))
            .order_by(PendingApproval.id)
        )
        return list(rows)


_REJECTABLE = [ApprovalStatus.PENDING.value, ApprovalStatus.AWAITING_EDIT.value]


async def reject_open_for_task(task_id: int) -> int:
    """Reject approvals still waiting on the user. RESOLVING rows already carry a decision that is
    being applied, so they are left for the approval gate to finish."""
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(PendingApproval.task_id == task_id, PendingApproval.status.in_(_REJECTABLE))
            .values(status=ApprovalStatus.REJECTED.value, resolved_at=utcnow())
        )
        await s.commit()
        return res.rowcount or 0


# --- sweep queries ------------------------------------------------------------------------


def _for_user(stmt, user_id: int | None):
    return stmt if user_id is None else stmt.where(PendingApproval.user_id == user_id)


async def overdue_open(now: datetime, user_id: int | None = None) -> list[PendingApproval]:
    """PENDING / AWAITING_EDIT approvals past their expiry (the expire wakeup was lost)."""
    stmt = select(PendingApproval).where(PendingApproval.status.in_(_REJECTABLE),
                                         PendingApproval.expires_at < now)
    async with Session() as s:
        return list(await s.scalars(_for_user(stmt, user_id).order_by(PendingApproval.id)))


async def stuck_resolving(cutoff: datetime, user_id: int | None = None) -> list[PendingApproval]:
    """RESOLVING since before `cutoff` and never started: the resume job was lost."""
    stmt = select(PendingApproval).where(
        PendingApproval.status == ApprovalStatus.RESOLVING.value,
        PendingApproval.started_at.is_(None),
        func.coalesce(PendingApproval.resolving_at, PendingApproval.created_at) < cutoff,
    )
    async with Session() as s:
        return list(await s.scalars(_for_user(stmt, user_id).order_by(PendingApproval.id)))


async def stale_may_have_run(cutoff: datetime, user_id: int | None = None) -> list[PendingApproval]:
    """EXECUTED, started before `cutoff`, never finished: the process died mid-action."""
    stmt = select(PendingApproval).where(
        PendingApproval.status == ApprovalStatus.EXECUTED.value,
        PendingApproval.started_at.is_not(None),
        PendingApproval.started_at < cutoff,
        PendingApproval.resolved_at.is_(None),
    )
    async with Session() as s:
        return list(await s.scalars(_for_user(stmt, user_id).order_by(PendingApproval.id)))


async def close_may_have_run(approval_id: int, note: str) -> bool:
    """Finish an EXECUTED-but-unfinished row as FAILED (outcome unknown) once the user was told."""
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(PendingApproval.id == approval_id,
                   PendingApproval.status == ApprovalStatus.EXECUTED.value,
                   PendingApproval.started_at.is_not(None),
                   PendingApproval.resolved_at.is_(None))
            .values(status=ApprovalStatus.FAILED.value, result=note, resolved_at=utcnow())
        )
        await s.commit()
        return (res.rowcount or 0) == 1


async def executed_after_stop(since: datetime, user_id: int | None = None) -> list[PendingApproval]:
    """Executed approvals of tasks that were cancelled or failed since `since`."""
    stmt = (
        select(PendingApproval)
        .join(Task, Task.id == PendingApproval.task_id)
        .where(PendingApproval.status == ApprovalStatus.EXECUTED.value,
               PendingApproval.resolved_at.is_not(None),
               Task.status.in_([TaskStatus.CANCELLED.value, TaskStatus.FAILED.value]),
               Task.finished_at >= since)
    )
    async with Session() as s:
        return list(await s.scalars(_for_user(stmt, user_id).order_by(PendingApproval.id)))
