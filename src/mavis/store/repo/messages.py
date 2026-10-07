from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from mavis.channels.formatting import strip_verbatim
from mavis.domain.messages import Role
from mavis.store.db import Session, utcnow
from mavis.store.models import Message, User


async def log(
    user_id: int, role: Role, content: str, proactive: bool = False, event_id: str | None = None
) -> bool:
    """Append to the conversation log. Returns False if `event_id` was already logged (retry).

    Text is stored as written: typography is applied once, at the channel, never to history. Only the
    verbatim render markers are dropped (they are a rendering hint, not content).
    """
    content = strip_verbatim(content)
    now = utcnow()
    async with Session() as s:
        if event_id and await s.scalar(select(Message.id).where(Message.event_id == event_id)):
            return False
        try:
            async with s.begin_nested():  # SAVEPOINT: a concurrent duplicate must not poison the session
                s.add(Message(user_id=user_id, role=role.value, content=content, proactive=proactive,
                              event_id=event_id, created_at=now))
                await s.flush()
        except IntegrityError:
            if event_id is None:
                raise
            return False  # lost the race on the unique event_id
        column = "last_user_msg_at" if role is Role.USER else "last_agent_msg_at"
        await s.execute(update(User).where(User.id == user_id).values({column: now}))
        await s.commit()
        return True


async def exists(event_id: str) -> bool:
    async with Session() as s:
        return await s.scalar(select(Message.id).where(Message.event_id == event_id)) is not None


async def recent(user_id: int, limit: int = 20) -> list[Message]:
    """The last `limit` messages, oldest first."""
    async with Session() as s:
        rows = await s.scalars(
            select(Message).where(Message.user_id == user_id)
            .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        )
        return list(reversed(list(rows)))


async def has_user_message_since(user_id: int, since: datetime) -> bool:
    async with Session() as s:
        found = await s.scalar(
            select(Message.id).where(Message.user_id == user_id, Message.role == Role.USER.value,
                                     Message.created_at > since).limit(1)
        )
        return found is not None


async def last_user_message_at(user_id: int) -> datetime | None:
    """Timestamp (aware UTC) of the user's latest message, or None."""
    async with Session() as s:
        ts = await s.scalar(
            select(func.max(Message.created_at)).where(Message.user_id == user_id,
                                                       Message.role == Role.USER.value)
        )
    return None if ts is None else ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


async def previous_user_event(user_id: int, event_id: str) -> tuple[str, timedelta] | None:
    """The user's turn just before the one logged as `event_id`: (its event id, how long before)."""
    async with Session() as s:
        here = (await s.execute(select(Message.id, Message.created_at).where(
            Message.user_id == user_id, Message.event_id == event_id))).first()
        if here is None:
            return None
        prev = (await s.execute(
            select(Message.event_id, Message.created_at).where(
                Message.user_id == user_id, Message.role == Role.USER.value, Message.id < here[0],
                Message.event_id.is_not(None))
            .order_by(Message.id.desc()).limit(1)
        )).first()
        if prev is None:
            return None
        gap = _aware(here[1]) - _aware(prev[1])
        return prev[0], gap


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
