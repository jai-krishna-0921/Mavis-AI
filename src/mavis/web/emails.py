"""Confirmed emails: the link between a verified Google address and a Mavis user."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mavis.store import db as dbm
from mavis.store.models import NativeGrant, UserEmail
from mavis.tools.integrations.native.base import NativeProvider


def norm(email: str) -> str:
    return (email or "").strip().lower()[:255]


async def confirm(user_id: int, email: str, source: str) -> bool:
    """Record `email` as the user's. False when another user already holds it (one address, one user)."""
    email = norm(email)
    if "@" not in email:
        return False
    async with dbm.Session() as s:
        row = await s.scalar(select(UserEmail).where(UserEmail.email == email))
        if row is not None:
            return row.user_id == user_id
        s.add(UserEmail(user_id=user_id, email=email, source=source))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            row = await s.scalar(select(UserEmail).where(UserEmail.email == email))
            return row is not None and row.user_id == user_id
        return True


async def user_for(email: str) -> int | None:
    """The user with this email confirmed: an explicit confirmation, or an active native Google grant."""
    email = norm(email)
    async with dbm.Session() as s:
        uid = await s.scalar(select(UserEmail.user_id).where(UserEmail.email == email))
        if uid is not None:
            return uid
        return await s.scalar(select(NativeGrant.user_id).where(
            NativeGrant.provider == NativeProvider.GOOGLE.value, NativeGrant.account_key == email,
            NativeGrant.status == "ACTIVE"))


async def primary(user_id: int) -> str | None:
    async with dbm.Session() as s:
        row = await s.scalar(select(UserEmail.email).where(UserEmail.user_id == user_id)
                             .order_by(UserEmail.id))
        if row:
            return row
        grant = await s.scalar(select(NativeGrant.account_key).where(
            NativeGrant.user_id == user_id, NativeGrant.provider == NativeProvider.GOOGLE.value))
        return grant
