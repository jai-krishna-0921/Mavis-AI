from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from mavis.config import get_settings
from mavis.store.db import Session
from mavis.store.models import User


async def get_or_create_by_chat(chat_id: int, name: str | None) -> tuple[User, bool]:
    async with Session() as s:
        user = await s.scalar(select(User).where(User.telegram_chat_id == chat_id))
        if user is not None:
            return user, False
        user = User(telegram_chat_id=chat_id, name=name, timezone=get_settings().default_timezone, state={})
        s.add(user)
        try:
            await s.commit()
        except IntegrityError:  # another worker created it concurrently
            await s.rollback()
            existing = await s.scalar(select(User).where(User.telegram_chat_id == chat_id))
            assert existing is not None
            return existing, False
        return user, True


async def get(user_id: int) -> User:
    async with Session() as s:
        return await s.get_one(User, user_id)


async def get_by_chat(chat_id: int) -> User | None:
    async with Session() as s:
        return await s.scalar(select(User).where(User.telegram_chat_id == chat_id))


async def update(user_id: int, **fields: Any) -> None:
    async with Session() as s:
        await s.execute(sa_update(User).where(User.id == user_id).values(**fields))
        await s.commit()


async def get_state(user_id: int) -> dict[str, Any]:
    async with Session() as s:
        user = await s.get_one(User, user_id)
        return dict(user.state or {})


async def set_state(user_id: int, **kv: Any) -> None:
    async with Session() as s:
        user = await s.get_one(User, user_id)
        user.state = {**(user.state or {}), **kv}
        await s.commit()


async def all_ids() -> list[int]:
    async with Session() as s:
        return list(await s.scalars(select(User.id).order_by(User.id)))
