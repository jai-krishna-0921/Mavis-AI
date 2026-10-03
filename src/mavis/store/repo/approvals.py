"""Pending approvals for outward / spend / destructive tool calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select, update

from mavis.domain.tasks import OPEN_APPROVAL_STATUSES, TERMINAL_APPROVAL_STATUSES, ApprovalStatus
from mavis.store.db import Session, utcnow
from mavis.store.models import PendingApproval

_OPEN = [s.value for s in OPEN_APPROVAL_STATUSES]


async def create(
    user_id: int, task_id: int | None, tool: str, arguments: dict, preview: str, expires_at: datetime
) -> int:
    async with Session() as s:
        a = PendingApproval(
            user_id=user_id, task_id=task_id, tool=tool, arguments=arguments, preview=preview,
            expires_at=expires_at, status=ApprovalStatus.PENDING.value,
        )
        s.add(a)
        await s.commit()
        return a.id


def args_hash(arguments: dict) -> str:
    canon = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


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
    async with Session() as s:
        res = await s.execute(
            update(PendingApproval)
            .where(PendingApproval.id == approval_id,
                   PendingApproval.status.in_([x.value for x in from_statuses]))
            .values(status=to.value)
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
