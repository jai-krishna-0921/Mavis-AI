"""Storage for the personal layer: versioned layer documents, learning suppressions, deterministic signals.

Every function takes the user id and filters on it: a user's rows are never readable through another's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select

from mavis.domain import timeutil
from mavis.store import db as dbm
from mavis.store.models import LearningSuppression, PersonalLayerRow, PersonalSignal

LAYER_HISTORY = 20  # versions kept per user; older ones are pruned on save


# --- the layer document ------------------------------------------------------------------------------


async def latest_layer(user_id: int) -> tuple[int, dict[str, Any], datetime] | None:
    async with dbm.Session() as s:
        row = await s.scalar(select(PersonalLayerRow).where(PersonalLayerRow.user_id == user_id)
                             .order_by(PersonalLayerRow.version.desc()).limit(1))
        return (row.version, dict(row.content or {}), row.created_at) if row else None


async def save_layer(user_id: int, content: dict[str, Any]) -> int:
    async with dbm.Session() as s:
        current = await s.scalar(
            select(func.max(PersonalLayerRow.version)).where(PersonalLayerRow.user_id == user_id))
        version = (current or 0) + 1
        s.add(PersonalLayerRow(user_id=user_id, version=version, content=content))
        await s.flush()
        await s.execute(delete(PersonalLayerRow).where(
            PersonalLayerRow.user_id == user_id, PersonalLayerRow.version <= version - LAYER_HISTORY))
        await s.commit()
        return version


async def delete_layers(user_id: int) -> None:
    async with dbm.Session() as s:
        await s.execute(delete(PersonalLayerRow).where(PersonalLayerRow.user_id == user_id))
        await s.commit()


# --- suppressions ------------------------------------------------------------------------------------


async def suppress(user_id: int, key: str, label: str = "") -> None:
    async with dbm.Session() as s:
        exists = await s.scalar(select(LearningSuppression.id).where(
            LearningSuppression.user_id == user_id, LearningSuppression.key == key))
        if exists is None:
            s.add(LearningSuppression(user_id=user_id, key=key[:240], label=label[:500]))
            await s.commit()


async def unsuppress(user_id: int, key: str) -> int:
    async with dbm.Session() as s:
        res = await s.execute(delete(LearningSuppression).where(
            LearningSuppression.user_id == user_id, LearningSuppression.key == key))
        await s.commit()
        return res.rowcount or 0


async def suppressed_keys(user_id: int) -> set[str]:
    async with dbm.Session() as s:
        rows = await s.scalars(
            select(LearningSuppression.key).where(LearningSuppression.user_id == user_id))
        return set(rows)


async def list_suppressions(user_id: int) -> list[tuple[str, str]]:
    async with dbm.Session() as s:
        rows = await s.scalars(select(LearningSuppression).where(LearningSuppression.user_id == user_id)
                               .order_by(LearningSuppression.id))
        return [(r.key, r.label) for r in rows]


# --- signals -----------------------------------------------------------------------------------------


async def record_signal(user_id: int, kind: str, key: str, source_ref: str, label: str, at: datetime,
                        meta: dict[str, Any] | None = None) -> bool:
    """Record one signal; the same (kind, key, source) twice is a no-op. True when it was new."""
    async with dbm.Session() as s:
        exists = await s.scalar(select(PersonalSignal.id).where(
            PersonalSignal.user_id == user_id, PersonalSignal.kind == kind, PersonalSignal.key == key[:200],
            PersonalSignal.source_ref == source_ref[:200]))
        if exists is not None:
            return False
        s.add(PersonalSignal(user_id=user_id, kind=kind, key=key[:200], source_ref=source_ref[:200],
                             label=label[:200], at=timeutil.ensure_utc(at), meta=meta or {}))
        await s.commit()
        return True


async def signals(user_id: int, kind: str, since: datetime | None = None) -> list[PersonalSignal]:
    async with dbm.Session() as s:
        q = select(PersonalSignal).where(PersonalSignal.user_id == user_id, PersonalSignal.kind == kind)
        if since is not None:
            q = q.where(PersonalSignal.at >= timeutil.ensure_utc(since))
        return list(await s.scalars(q.order_by(PersonalSignal.at)))


async def delete_signals_by_source(user_id: int, prefix: str) -> int:
    if not prefix.strip():
        return 0
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    async with dbm.Session() as s:
        res = await s.execute(delete(PersonalSignal).where(
            PersonalSignal.user_id == user_id, PersonalSignal.source_ref.like(like, escape="\\")))
        await s.commit()
        return res.rowcount or 0


async def delete_signals_by_key(user_id: int, kind: str, key: str) -> int:
    async with dbm.Session() as s:
        res = await s.execute(delete(PersonalSignal).where(
            PersonalSignal.user_id == user_id, PersonalSignal.kind == kind, PersonalSignal.key == key[:200]))
        await s.commit()
        return res.rowcount or 0


async def prune_signals(user_id: int, before: datetime) -> int:
    """Interaction history older than the cutoff stops counting (frequency is recent behaviour)."""
    async with dbm.Session() as s:
        res = await s.execute(delete(PersonalSignal).where(
            PersonalSignal.user_id == user_id, PersonalSignal.kind == "interaction",
            PersonalSignal.at < timeutil.ensure_utc(before)))
        await s.commit()
        return res.rowcount or 0


async def delete_signals(user_id: int, ids: list[int]) -> int:
    if not ids:
        return 0
    async with dbm.Session() as s:
        res = await s.execute(delete(PersonalSignal).where(
            PersonalSignal.user_id == user_id, PersonalSignal.id.in_(ids)))
        await s.commit()
        return res.rowcount or 0
