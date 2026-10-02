"""Versioned profile cards: every save is a new row; the latest version wins."""

from __future__ import annotations

from sqlalchemy import func, select

from zento.memory.profile import ProfileCard
from zento.store import db as dbm
from zento.store.models import ProfileCardRow


def _to_card(row: ProfileCardRow) -> ProfileCard:
    return ProfileCard.model_validate({**(row.content or {}), "version": row.version})


async def get(user_id: int) -> ProfileCard:
    async with dbm.Session() as s:
        row = await s.scalar(
            select(ProfileCardRow).where(ProfileCardRow.user_id == user_id)
            .order_by(ProfileCardRow.version.desc()).limit(1)
        )
        return _to_card(row) if row else ProfileCard()


async def save(user_id: int, card: ProfileCard) -> int:
    async with dbm.Session() as s:
        current = await s.scalar(
            select(func.max(ProfileCardRow.version)).where(ProfileCardRow.user_id == user_id)
        )
        version = (current or 0) + 1
        s.add(ProfileCardRow(user_id=user_id, version=version,
                             content=card.model_dump(mode="json", exclude={"version"})))
        await s.commit()
        return version


async def history(user_id: int, limit: int = 5) -> list[ProfileCard]:
    async with dbm.Session() as s:
        rows = await s.scalars(
            select(ProfileCardRow).where(ProfileCardRow.user_id == user_id)
            .order_by(ProfileCardRow.version.desc()).limit(limit)
        )
        return [_to_card(r) for r in rows]
