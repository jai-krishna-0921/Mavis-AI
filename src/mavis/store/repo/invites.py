"""Invite code rows. Only hashes are stored; the plaintext is returned once by mint()."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from mavis.access import UserTier
from mavis.access.codes import InviteError, code_hash, generate_code, hint, normalize
from mavis.config import get_settings
from mavis.store.db import Session, utcnow
from mavis.store.models import InviteCode, InviteRedemption


def _active(now: datetime):
    return (InviteCode.revoked_at.is_(None)) & (InviteCode.expires_at > now)


async def mint(*, created_by: int | None, uses: int = 1, days: int | None = None, tier: str = "standard",
               tz: str | None = None, currency: str | None = None, label: str = "") -> tuple[InviteCode, str]:
    s = get_settings()
    if tier not in {x.value for x in UserTier} or tier == UserTier.OWNER.value:
        raise InviteError("invalid")  # unknown tier, or owner: an invite never mints an owner
    if uses < 1 or uses > s.invite_max_uses:
        raise InviteError("limit")
    now = utcnow()
    async with Session() as session:
        active = await session.scalar(select(func.count(InviteCode.id)).where(_active(now)))
        if (active or 0) >= s.invite_max_active:
            raise InviteError("limit")
        plain = generate_code()
        row = InviteCode(code_hash=code_hash(plain), code_hint=hint(plain), label=label[:120], tier=tier,
                         max_uses=uses, uses=0,
                         expires_at=now + timedelta(days=days or s.invite_default_days),
                         created_by_user_id=created_by, default_timezone=tz,
                         default_currency=currency.upper() if currency else None)
        session.add(row)
        await session.commit()
        return row, plain


async def list_active(now: datetime | None = None) -> list[InviteCode]:
    async with Session() as s:
        rows = await s.scalars(select(InviteCode).where(_active(now or utcnow())).order_by(InviteCode.id))
        return list(rows)


async def revoke(hint_or_id: str) -> InviteCode | None:
    key = hint_or_id.strip().upper()
    async with Session() as s:
        # A hint can be all digits (it is the last 4 characters of a code), so match the hint first and
        # fall back to the numeric id only when no code has that hint.
        row = await s.scalar(select(InviteCode).where(InviteCode.code_hint == key,
                                                      InviteCode.revoked_at.is_(None)))
        if row is None and key.isdigit():
            row = await s.scalar(select(InviteCode).where(InviteCode.id == int(key),
                                                          InviteCode.revoked_at.is_(None)))
        if row is None:
            return None
        row.revoked_at = utcnow()
        await s.commit()
        return row


async def redeem(code: str, user_id: int, now: datetime) -> InviteCode | None:
    """Use one redemption of `code` for `user_id`, atomically. Unknown, revoked, expired and exhausted
    codes all return None (no oracle). The conditional UPDATE is the race guard on every database; on
    Postgres the row lock comes from the UPDATE itself."""
    canonical = normalize(code)
    if canonical is None:
        return None
    digest = code_hash(canonical)
    async with Session() as s:
        # Idempotent per (invite, user): a retry after a crash between redeem and activate gets the invite
        # back without burning a second use. It is the user's own earlier redemption, so no oracle either.
        prior = await s.scalar(
            select(InviteCode).join(InviteRedemption, InviteRedemption.invite_id == InviteCode.id)
            .where(InviteCode.code_hash == digest, InviteRedemption.user_id == user_id))
        if prior is not None:
            return prior
        res = await s.execute(
            update(InviteCode)
            .where(InviteCode.code_hash == digest, _active(now), InviteCode.uses < InviteCode.max_uses)
            .values(uses=InviteCode.uses + 1)
        )
        if res.rowcount != 1:
            await s.rollback()
            return None
        row = await s.scalar(select(InviteCode).where(InviteCode.code_hash == digest))
        s.add(InviteRedemption(invite_id=row.id, user_id=user_id, redeemed_at=now))
        await s.commit()
        return row


async def redemptions_for(invite_id: int) -> list[InviteRedemption]:
    async with Session() as s:
        rows = await s.scalars(select(InviteRedemption).where(InviteRedemption.invite_id == invite_id)
                               .order_by(InviteRedemption.id))
        return list(rows)
