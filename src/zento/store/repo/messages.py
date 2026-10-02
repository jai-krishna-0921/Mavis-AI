from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from zento.domain.messages import Role
from zento.store.db import Session, utcnow
from zento.store.models import Message, User


async def log(
    user_id: int, role: Role, content: str, proactive: bool = False, event_id: str | None = None
) -> bool:
    """Append to the conversation log. Returns False if `event_id` was already logged (retry)."""
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


async def recent(user_id: int, limit: int = 20) -> list[Message]:
    """The last `limit` messages, oldest first."""
    async with Session() as s:
        rows = await s.scalars(
            select(Message).where(Message.user_id == user_id)
            .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        )
        return list(reversed(list(rows)))
