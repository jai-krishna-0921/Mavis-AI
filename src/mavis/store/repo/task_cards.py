"""Persisted progress card state (one row per user task)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import update

from mavis.store.db import Session, utcnow
from mavis.store.models import TaskCard


async def get(task_id: int) -> TaskCard | None:
    async with Session() as s:
        return await s.get(TaskCard, task_id)


async def save(task_id: int, user_id: int, chat_id: int, message_id: int | None, state: dict[str, Any],
               final: bool) -> None:
    """Upsert the card. A known message id is never cleared and a final card never becomes live again."""
    async with Session() as s:
        row = await s.get(TaskCard, task_id, with_for_update=True)
        if row is None:
            s.add(TaskCard(task_id=task_id, user_id=user_id, chat_id=chat_id, message_id=message_id,
                           state=state, final=final, last_edit_at=utcnow()))
        else:
            row.message_id = message_id if message_id is not None else row.message_id
            row.state, row.final, row.last_edit_at = state, final or row.final, utcnow()
        await s.commit()


async def set_message(task_id: int, message_id: int) -> None:
    async with Session() as s:
        await s.execute(update(TaskCard).where(TaskCard.task_id == task_id).values(message_id=message_id))
        await s.commit()
