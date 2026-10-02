"""Exactly-once guard for side-effecting handlers (in addition to bus-level dedupe)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mavis.store.db import Session, utcnow
from mavis.store.models import ProcessedEvent


async def claim(session: AsyncSession, event_id: str) -> bool:
    """Record event_id as processed within the caller's transaction. False if already recorded."""
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    stmt = insert(ProcessedEvent).values(id=event_id, processed_at=utcnow()).on_conflict_do_nothing(
        index_elements=["id"]
    )
    res = await session.execute(stmt)
    return res.rowcount == 1


async def seen(event_id: str) -> bool:
    async with Session() as s:
        return await s.scalar(select(ProcessedEvent.id).where(ProcessedEvent.id == event_id)) is not None


async def record(event_id: str) -> None:
    """Mark event_id processed in its own transaction (no-op if already recorded)."""
    async with Session() as s:
        await claim(s, event_id)
        await s.commit()
