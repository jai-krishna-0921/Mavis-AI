"""Rolling summaries of conversation older than the working-memory window."""

from __future__ import annotations

from sqlalchemy import select

from mavis.store import db as dbm
from mavis.store.models import ConversationSummary, Message


async def latest(user_id: int) -> ConversationSummary | None:
    async with dbm.Session() as s:
        return await s.scalar(
            select(ConversationSummary).where(ConversationSummary.user_id == user_id)
            .order_by(ConversationSummary.id.desc()).limit(1)
        )


async def add(user_id: int, upto_message_id: int, text: str) -> None:
    async with dbm.Session() as s:
        s.add(ConversationSummary(user_id=user_id, upto_message_id=upto_message_id, summary=text))
        await s.commit()


async def messages_outside_window(user_id: int, after_id: int, window: int) -> list[Message]:
    """Messages newer than `after_id` that are older than the newest `window` messages."""
    async with dbm.Session() as s:
        cutoff = await s.scalar(
            select(Message.id).where(Message.user_id == user_id)
            .order_by(Message.id.desc()).offset(window - 1).limit(1)
        )
        if cutoff is None:
            return []
        rows = await s.scalars(
            select(Message).where(Message.user_id == user_id, Message.id > after_id, Message.id < cutoff)
            .order_by(Message.id)
        )
        return list(rows)
