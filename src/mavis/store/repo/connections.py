"""Pending connection requests (a connect link sent, outcome unknown)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from mavis.domain import timeutil
from mavis.domain.integrations import PendingStatus
from mavis.domain.policy import Capability
from mavis.store.db import Session
from mavis.store.models import ConnectionPending


def _now(now: datetime | None) -> datetime:
    return now or timeutil.now()


async def create_pending(
    user_id: int,
    capability: Capability,
    reason: str,
    task_id: str | None,
    now: datetime | None = None,
) -> int:
    async with Session() as s:
        row = ConnectionPending(
            user_id=user_id,
            capability=capability.value,
            reason=reason,
            task_id=task_id,
            status=PendingStatus.PENDING.value,
            created_at=_now(now),
        )
        s.add(row)
        await s.commit()
        return row.id


async def get_pending(pending_id: int) -> ConnectionPending | None:
    async with Session() as s:
        return await s.get(ConnectionPending, pending_id)


async def open_for(user_id: int, capability: Capability) -> list[ConnectionPending]:
    async with Session() as s:
        rows = await s.scalars(
            select(ConnectionPending)
            .where(
                ConnectionPending.user_id == user_id,
                ConnectionPending.capability == capability.value,
                ConnectionPending.status == PendingStatus.PENDING.value,
            )
            .order_by(ConnectionPending.id)
        )
        return list(rows)


async def has_open(user_id: int) -> bool:
    """The user started a connect flow (any capability) that is still waiting."""
    async with Session() as s:
        return await s.scalar(select(ConnectionPending.id).where(
            ConnectionPending.user_id == user_id,
            ConnectionPending.status == PendingStatus.PENDING.value).limit(1)) is not None


async def latest_open(user_id: int, capability: Capability) -> ConnectionPending | None:
    rows = await open_for(user_id, capability)
    return rows[-1] if rows else None


async def resolve(pending_id: int, status: PendingStatus, now: datetime | None = None) -> None:
    async with Session() as s:
        await s.execute(
            update(ConnectionPending)
            .where(ConnectionPending.id == pending_id)
            .values(status=status.value, resolved_at=_now(now))
        )
        await s.commit()
