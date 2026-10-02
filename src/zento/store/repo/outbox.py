"""Transactional outbox: every message to the user is a row first, delivered by OutboxSender."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from zento.domain.messages import Button, Outbound
from zento.store.db import Session, utcnow
from zento.store.models import OutboxMessage

LEASE = timedelta(seconds=300)  # a claimed row becomes due again if its sender dies
_DELIVERABLE = ("pending", "sending")


async def enqueue(session: AsyncSession, msg: Outbound) -> int:
    """Insert within the caller's transaction. Same dedupe_key => existing row id, no new row."""
    if msg.dedupe_key:
        existing = await session.scalar(
            select(OutboxMessage.id).where(OutboxMessage.dedupe_key == msg.dedupe_key)
        )
        if existing is not None:
            return existing
    row = OutboxMessage(
        user_id=msg.user_id,
        text=msg.text,
        buttons=[[b.model_dump() for b in row] for row in msg.buttons],
        document_path=msg.document_path,
        proactive=msg.proactive,
        dedupe_key=msg.dedupe_key,
    )
    try:
        async with session.begin_nested():  # SAVEPOINT: keeps the caller's transaction usable on a race
            session.add(row)
            await session.flush()
    except IntegrityError:
        if not msg.dedupe_key:
            raise
        existing = (
            await session.execute(select(OutboxMessage.id).where(OutboxMessage.dedupe_key == msg.dedupe_key))
        ).scalar_one()
        return existing
    return row.id


async def enqueue_now(msg: Outbound) -> int:
    async with Session() as s:
        outbox_id = await enqueue(s, msg)
        await s.commit()
        return outbox_id


async def texts_with_dedupe_prefix(prefix: str) -> list[str]:
    """Texts of every outbox row whose dedupe_key starts with `prefix`, in enqueue order."""
    async with Session() as s:
        rows = await s.scalars(
            select(OutboxMessage.text)
            .where(OutboxMessage.dedupe_key.startswith(prefix, autoescape=True))
            .order_by(OutboxMessage.id)
        )
        return list(rows)


async def due(now: datetime, limit: int = 20) -> list[OutboxMessage]:
    """Rows ready to send. A user's row is held back while an earlier row of theirs is still
    pending/sending (retrying or rate-limited), so bubbles are delivered in order per user."""
    earlier = aliased(OutboxMessage)
    blocked = (
        select(earlier.id)
        .where(earlier.user_id == OutboxMessage.user_id, earlier.id < OutboxMessage.id,
               earlier.status.in_(_DELIVERABLE))
        .exists()
    )
    async with Session() as s:
        rows = await s.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.status.in_(_DELIVERABLE), OutboxMessage.next_attempt_at <= now, ~blocked)
            .order_by(OutboxMessage.id).limit(limit)
        )
        return list(rows)


async def claim(outbox_id: int, now: datetime) -> bool:
    """Atomically take a row for delivery. Only one sender wins."""
    async with Session() as s:
        res = await s.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == outbox_id, OutboxMessage.status.in_(_DELIVERABLE),
                   OutboxMessage.next_attempt_at <= now)
            .values(status="sending", next_attempt_at=now + LEASE)
        )
        await s.commit()
        return res.rowcount == 1


async def mark_sent(outbox_id: int, provider_ids: list[int]) -> None:
    async with Session() as s:
        await s.execute(
            update(OutboxMessage).where(OutboxMessage.id == outbox_id)
            .values(status="sent", sent_at=utcnow(), provider_message_ids=provider_ids, last_error=None)
        )
        await s.commit()


async def mark_retry(
    outbox_id: int, error: str, next_attempt_at: datetime, count_attempt: bool = True
) -> None:
    values: dict = {"status": "pending", "last_error": error, "next_attempt_at": next_attempt_at}
    if count_attempt:
        values["attempts"] = OutboxMessage.attempts + 1
    async with Session() as s:
        await s.execute(update(OutboxMessage).where(OutboxMessage.id == outbox_id).values(**values))
        await s.commit()


async def mark_failed(outbox_id: int, error: str) -> None:
    async with Session() as s:
        await s.execute(
            update(OutboxMessage).where(OutboxMessage.id == outbox_id)
            .values(status="failed", last_error=error, attempts=OutboxMessage.attempts + 1)
        )
        await s.commit()


def to_outbound(row: OutboxMessage) -> Outbound:
    return Outbound(
        user_id=row.user_id,
        text=row.text,
        buttons=[[Button(**b) for b in r] for r in row.buttons or []],
        document_path=row.document_path,
        proactive=row.proactive,
        dedupe_key=row.dedupe_key,
    )
