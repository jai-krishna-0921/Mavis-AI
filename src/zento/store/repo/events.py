"""Exactly-once guard for side-effecting handlers (in addition to bus-level dedupe)."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from zento.store.db import utcnow
from zento.store.models import ProcessedEvent


async def claim(session: AsyncSession, event_id: str) -> bool:
    """Record event_id as processed within the caller's transaction. False if already recorded."""
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    stmt = insert(ProcessedEvent).values(id=event_id, processed_at=utcnow()).on_conflict_do_nothing(
        index_elements=["id"]
    )
    res = await session.execute(stmt)
    return res.rowcount == 1
