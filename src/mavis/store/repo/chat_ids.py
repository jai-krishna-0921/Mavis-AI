"""Message ids per Telegram chat (ids only), the input to /clear. Best effort everywhere: a failure to note an
id must never fail a send or a turn."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from mavis.store.db import Session, utcnow
from mavis.store.models import ChatMessageId

KEEP = timedelta(hours=72)  # Telegram refuses deletes after 48h, so older ids are dead weight


async def record(chat_id: int, message_ids: Iterable[int], direction: str,
                 at: datetime | None = None) -> None:
    ids = sorted({int(m) for m in message_ids if m})
    if not ids:
        return
    at = at or utcnow()
    async with Session() as s:
        have = set(await s.scalars(select(ChatMessageId.message_id).where(
            ChatMessageId.chat_id == chat_id, ChatMessageId.message_id.in_(ids))))
        fresh = [m for m in ids if m not in have]
        for m in fresh:
            s.add(ChatMessageId(chat_id=chat_id, message_id=m, direction=direction, sent_at=at))
        try:
            await s.commit()
        except IntegrityError:  # a concurrent writer noted the same id first
            await s.rollback()
        await s.execute(delete(ChatMessageId).where(ChatMessageId.chat_id == chat_id,
                                                    ChatMessageId.sent_at < at - KEEP))
        await s.commit()


async def since(chat_id: int, cutoff: datetime) -> list[int]:
    """Known ids sent at or after `cutoff`, newest first."""
    async with Session() as s:
        rows = await s.scalars(select(ChatMessageId.message_id).where(
            ChatMessageId.chat_id == chat_id, ChatMessageId.sent_at >= cutoff)
            .order_by(ChatMessageId.message_id.desc()))
        return list(rows)


async def latest(chat_id: int) -> int | None:
    async with Session() as s:
        return await s.scalar(select(func.max(ChatMessageId.message_id)).where(
            ChatMessageId.chat_id == chat_id))


async def forget(chat_id: int, message_ids: Iterable[int] | None = None) -> int:
    """Drop the ids of a chat (all, or just `message_ids`). Returns how many rows went."""
    stmt = delete(ChatMessageId).where(ChatMessageId.chat_id == chat_id)
    if message_ids is not None:
        stmt = stmt.where(ChatMessageId.message_id.in_(list(message_ids)))
    async with Session() as s:
        res = await s.execute(stmt)
        await s.commit()
        return res.rowcount or 0


async def forget_before(chat_id: int, cutoff: datetime) -> int:
    async with Session() as s:
        res = await s.execute(delete(ChatMessageId).where(ChatMessageId.chat_id == chat_id,
                                                          ChatMessageId.sent_at < cutoff))
        await s.commit()
        return res.rowcount or 0
