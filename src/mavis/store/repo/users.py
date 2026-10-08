from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from mavis.config import get_settings
from mavis.store.db import Session
from mavis.store.models import User


async def get_or_create_by_chat(chat_id: int, name: str | None, *,
                                telegram_user_id: int | None = None) -> tuple[User, bool]:
    async with Session() as s:
        user = await s.scalar(select(User).where(User.telegram_chat_id == chat_id))
        if user is not None:
            return user, False
        # New rows start pending (Phase 11): the access gate activates them on redemption. In
        # ACCESS_MODE=allowlist the gate is not enforced, so this changes nothing for the owner.
        user = User(telegram_chat_id=chat_id, telegram_user_id=telegram_user_id, name=name,
                    timezone=get_settings().default_timezone, state={}, status="pending")
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
    await update_state(user_id, kv)


async def all_ids() -> list[int]:
    async with Session() as s:
        return list(await s.scalars(select(User.id).order_by(User.id)))


async def update_state(user_id: int, patch: dict) -> dict:
    """Shallow merge `patch` into users.state and return the merged dict."""
    async with Session() as s:
        # Row lock: concurrent writers of different keys (poller cursors, attention, connect flow)
        # must not overwrite each other's read-modify-write. No-op on SQLite (single writer).
        u = await s.get_one(User, user_id, with_for_update=True, populate_existing=True)
        merged = {**(u.state or {}), **patch}
        u.state = merged
        await s.commit()
        return merged


async def update_nested(user_id: int, key: str, patch: dict) -> dict:
    """Shallow merge `patch` into the dict at users.state[key], inside the same row lock as update_state,
    and return the merged sub-dict. Concurrent writers of different sub-keys keep each other's values."""
    return await modify_nested(user_id, key, lambda current: {**current, **patch})


async def modify_nested(user_id: int, key: str, change: Callable[[dict], dict]) -> dict:
    """Replace the dict at users.state[key] with `change(current)`, computed inside the row lock, so a
    read-modify-write (append to a list, advance a cursor) never loses a concurrent writer's update."""
    async with Session() as s:
        u = await s.get_one(User, user_id, with_for_update=True, populate_existing=True)
        state = dict(u.state or {})
        current = state.get(key)
        merged = change(dict(current) if isinstance(current, dict) else {})
        state[key] = merged
        u.state = state
        await s.commit()
        return merged


async def purge_strangers(cutoff: datetime, keep_chat_ids: frozenset[int] = frozenset()) -> int:
    """Delete pending users (never activated, never redeemed a code) created before `cutoff`, with every row
    that points at them. Bounds the growth of stranger rows in invite mode. Returns how many were removed."""
    from sqlalchemy import delete

    from mavis.store.models import Base, InviteRedemption

    async with Session() as s:
        ids = list(await s.scalars(
            select(User.id).where(User.status == "pending", User.created_at < cutoff,
                                  User.activated_at.is_(None), User.is_test.is_(False),
                                  User.id.notin_(select(InviteRedemption.user_id)))))
        if keep_chat_ids:
            kept = set(await s.scalars(select(User.id).where(User.telegram_chat_id.in_(keep_chat_ids))))
            ids = [i for i in ids if i not in kept]
        if not ids:
            return 0
        for table in reversed(Base.metadata.sorted_tables):
            if table.name != "users" and "user_id" in table.c:
                await s.execute(delete(table).where(table.c.user_id.in_(ids)))
        await s.execute(delete(User).where(User.id.in_(ids)))
        await s.commit()
        return len(ids)
