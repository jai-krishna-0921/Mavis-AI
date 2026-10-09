"""Machine data access: sessions, usage, workspace file metadata and quota overrides (Phase 12)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from mavis.machine.ports import WorkspaceFile
from mavis.store.db import Session, utcnow
from mavis.store.models import ComputeUsage, MachineSession, User, UserQuota, WorkspaceFileRow

# --- workspace file metadata ------------------------------------------------------------------


async def upsert_file(f: WorkspaceFile) -> None:
    async with Session() as s:
        row = await s.scalar(select(WorkspaceFileRow).where(WorkspaceFileRow.user_id == f.user_id,
                                                            WorkspaceFileRow.path == f.path))
        now = utcnow()
        if row is None:
            s.add(WorkspaceFileRow(user_id=f.user_id, path=f.path, size=f.size, sha256=f.sha256,
                                   cls=f.cls.value, provenance=f.provenance.value, task_id=f.task_id,
                                   created_at=now, updated_at=now))
        else:
            row.size, row.sha256, row.cls = f.size, f.sha256, f.cls.value
            row.provenance, row.task_id = f.provenance.value, f.task_id
            row.updated_at, row.deleted_at = now, None
        await s.commit()


async def get_file(user_id: int, path: str) -> WorkspaceFileRow | None:
    async with Session() as s:
        return await s.scalar(select(WorkspaceFileRow).where(WorkspaceFileRow.user_id == user_id,
                                                             WorkspaceFileRow.path == path))


async def list_files(user_id: int, prefix: str = "") -> list[WorkspaceFileRow]:
    """Live rows only, ordered by path."""
    async with Session() as s:
        q = select(WorkspaceFileRow).where(WorkspaceFileRow.user_id == user_id,
                                           WorkspaceFileRow.deleted_at.is_(None))
        if prefix:
            q = q.where(WorkspaceFileRow.path.startswith(prefix, autoescape=True))
        return list(await s.scalars(q.order_by(WorkspaceFileRow.path)))


async def soft_delete_file(user_id: int, path: str) -> None:
    async with Session() as s:
        await s.execute(update(WorkspaceFileRow).where(WorkspaceFileRow.user_id == user_id,
                                                       WorkspaceFileRow.path == path)
                        .values(deleted_at=utcnow()))
        await s.commit()


async def delete_file_rows(user_id: int, paths: list[str]) -> None:
    async with Session() as s:
        for i in range(0, len(paths), 500):
            await s.execute(delete(WorkspaceFileRow).where(WorkspaceFileRow.user_id == user_id,
                                                           WorkspaceFileRow.path.in_(paths[i:i + 500])))
        await s.commit()


async def live_bytes(user_id: int) -> int:
    async with Session() as s:
        total = await s.scalar(select(func.coalesce(func.sum(WorkspaceFileRow.size), 0))
                               .where(WorkspaceFileRow.user_id == user_id,
                                      WorkspaceFileRow.deleted_at.is_(None)))
        return int(total or 0)


# --- sessions ---------------------------------------------------------------------------------


async def open_session(*, user_id: int, task_id: int, kind: str, backend: str, session_id: str,
                       deadline_at: datetime | None) -> int:
    async with Session() as s:
        row = MachineSession(user_id=user_id, task_id=task_id, kind=kind, backend=backend,
                             session_id=session_id, status="open", deadline_at=deadline_at)
        s.add(row)
        await s.commit()
        return row.id


_reserve_locks: dict[int, asyncio.Lock] = {}


async def reserve_session(*, user_id: int, task_id: int, kind: str, backend: str,
                          deadline_at: datetime | None,
                          max_open: int) -> int | None:
    """Atomically take one of the user's session slots: count their other open sessions, insert a placeholder
    row in one transaction under the user's row lock (and a process lock where the database has no row locks).
    Returns the row id, or None when the user is at the limit. `bind_session` fills in the real id."""
    async with _reserve_locks.setdefault(user_id, asyncio.Lock()):
        async with Session() as s:
            await s.execute(select(User.id).where(User.id == user_id).with_for_update())
            busy = int(await s.scalar(select(func.count()).select_from(MachineSession).where(
                MachineSession.user_id == user_id, MachineSession.status == "open",
                MachineSession.task_id != task_id)) or 0)
            if busy >= max_open:
                await s.rollback()
                return None
            row = MachineSession(user_id=user_id, task_id=task_id, kind=kind, backend=backend,
                                 session_id=f"pending-{uuid.uuid4().hex}", status="open",
                                 deadline_at=deadline_at)
            s.add(row)
            await s.commit()
            return row.id


async def bind_session(row_id: int, session_id: str) -> None:
    async with Session() as s:
        await s.execute(update(MachineSession).where(MachineSession.id == row_id)
                        .values(session_id=session_id))
        await s.commit()


async def drop_session(row_id: int) -> None:
    async with Session() as s:
        await s.execute(delete(MachineSession).where(MachineSession.id == row_id))
        await s.commit()


async def close_session(session_id: str, status: str, wall_s: float, cost: float) -> bool:
    """Only an open row is closed, so cancel followed by release (or the reaper) never double counts."""
    async with Session() as s:
        res = await s.execute(update(MachineSession)
                              .where(MachineSession.session_id == session_id, MachineSession.status == "open")
                              .values(status=status, closed_at=utcnow(), wall_s=wall_s, est_cost_usd=cost))
        await s.commit()
        return bool(res.rowcount)


async def sessions_for_task(task_id: int, status: str | None = "open") -> list[MachineSession]:
    async with Session() as s:
        q = select(MachineSession).where(MachineSession.task_id == task_id)
        if status is not None:
            q = q.where(MachineSession.status == status)
        return list(await s.scalars(q.order_by(MachineSession.id)))


async def open_sessions(*, before: datetime | None = None) -> list[MachineSession]:
    async with Session() as s:
        q = select(MachineSession).where(MachineSession.status == "open")
        if before is not None:
            q = q.where(MachineSession.opened_at < before)
        return list(await s.scalars(q.order_by(MachineSession.id)))


async def open_count_for_user(user_id: int, exclude_task: int | None = None) -> int:
    async with Session() as s:
        q = select(func.count()).select_from(MachineSession).where(MachineSession.user_id == user_id,
                                                                    MachineSession.status == "open")
        if exclude_task is not None:
            q = q.where(MachineSession.task_id != exclude_task)
        return int(await s.scalar(q) or 0)


# --- usage and quotas -------------------------------------------------------------------------


async def record_usage(user_id: int, day: date, provider: str, kind: str, *, task_id: int | None,
                       session_id: str, wall_s: float, est_cost_usd: float) -> None:
    """One row per session: a repeat for the same session id is ignored."""
    async with Session() as s:
        s.add(ComputeUsage(user_id=user_id, day=day, provider=provider, kind=kind, task_id=task_id,
                           session_id=session_id, wall_s=wall_s, est_cost_usd=est_cost_usd))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()


async def minutes_on(user_id: int, day: date) -> float:
    async with Session() as s:
        total = await s.scalar(select(func.coalesce(func.sum(ComputeUsage.wall_s), 0.0))
                               .where(ComputeUsage.user_id == user_id, ComputeUsage.day == day))
        return float(total or 0.0) / 60


async def spend_between(user_id: int, start_day: date, end_day: date) -> float:
    async with Session() as s:
        total = await s.scalar(select(func.coalesce(func.sum(ComputeUsage.est_cost_usd), 0.0))
                               .where(ComputeUsage.user_id == user_id, ComputeUsage.day >= start_day,
                                      ComputeUsage.day <= end_day))
        return float(total or 0.0)


async def quota_override(user_id: int, key: str) -> float | None:
    async with Session() as s:
        row = await s.scalar(select(UserQuota).where(UserQuota.user_id == user_id, UserQuota.key == key))
        return None if row is None else float(row.value)


async def set_quota(user_id: int, key: str, value: float) -> None:
    async with Session() as s:
        row = await s.scalar(select(UserQuota).where(UserQuota.user_id == user_id, UserQuota.key == key))
        if row is None:
            s.add(UserQuota(user_id=user_id, key=key, value=float(value)))
        else:
            row.value, row.updated_at = float(value), utcnow()
        await s.commit()


async def purge_user(user_id: int) -> None:
    """Rows only; the bytes are the store's job."""
    async with Session() as s:
        for model in (WorkspaceFileRow, UserQuota, ComputeUsage, MachineSession):
            await s.execute(delete(model).where(model.user_id == user_id))
        await s.commit()
