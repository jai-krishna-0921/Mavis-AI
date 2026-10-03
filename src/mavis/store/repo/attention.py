"""Attention observations and preferences (spec attention section 11)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from mavis.store.db import Session, utcnow
from mavis.store.models import AttentionObservation, AttentionPref

PENDING, DONE = "pending", "done"
ORIGIN_LIVE, ORIGIN_BACKFILL = "live", "backfill"
QUEUED = "queued"
_Obs = AttentionObservation


def _by_message(user_id: int, message_id: str):
    return select(_Obs).where(_Obs.user_id == user_id, _Obs.message_id == message_id)


async def insert_pending(
    user_id: int,
    message_id: str,
    *,
    thread_id: str,
    origin: str,
    sender_domain: str,
    sender_name: str,
    received_at: datetime,
    payload: dict[str, Any],
) -> tuple[AttentionObservation, bool]:
    """One row per (user, message id): webhook and poller copies, retries and backfill collapse here."""
    async with Session() as s:
        existing = await s.scalar(_by_message(user_id, message_id))
        if existing is not None:
            return existing, False
        row = _Obs(
            user_id=user_id,
            message_id=message_id,
            thread_id=thread_id,
            origin=origin,
            status=PENDING,
            attempts=0,
            method="",
            sender_domain=sender_domain,
            sender_name=sender_name,
            kind="other",
            needs_user=False,
            verdict=PENDING,
            urgency=0,
            score=0.0,
            reasons=[],
            facts={},
            summary="",
            action="",
            pending_payload=payload,
            delivery="none",
            received_at=received_at,
            created_at=utcnow(),
        )
        s.add(row)
        try:
            await s.commit()
        except IntegrityError:  # a concurrent insert of the same message won
            await s.rollback()
            again = await s.scalar(_by_message(user_id, message_id))
            assert again is not None
            return again, False
        await s.refresh(row)
        return row, True


async def get(obs_id: int) -> AttentionObservation | None:
    async with Session() as s:
        return await s.get(_Obs, obs_id)


async def note_attempt(obs_id: int, at: datetime) -> None:
    async with Session() as s:
        await s.execute(
            update(_Obs).where(_Obs.id == obs_id).values(attempts=_Obs.attempts + 1, last_attempt_at=at)
        )
        await s.commit()


async def attempts_since(user_id: int, since: datetime) -> int:
    """Rows whose latest LLM attempt is inside the window: the per-user understanding budget."""
    async with Session() as s:
        n = await s.scalar(
            select(func.count())
            .select_from(_Obs)
            .where(_Obs.user_id == user_id, _Obs.last_attempt_at >= since)
        )
    return int(n or 0)


async def pending(user_id: int, limit: int = 10) -> list[AttentionObservation]:
    live_first = case((_Obs.origin == ORIGIN_LIVE, 0), else_=1)
    async with Session() as s:
        rows = await s.scalars(
            select(_Obs)
            .where(_Obs.user_id == user_id, _Obs.status == PENDING)
            .order_by(live_first, _Obs.received_at, _Obs.id)
            .limit(limit)
        )
        return list(rows)


async def pending_count(user_id: int) -> int:
    async with Session() as s:
        n = await s.scalar(
            select(func.count()).select_from(_Obs).where(_Obs.user_id == user_id, _Obs.status == PENDING)
        )
    return int(n or 0)


async def finish(obs_id: int, **fields: Any) -> bool:
    """pending -> done exactly once. Clears the snippet (no bodies at rest)."""
    values = {**fields, "status": DONE, "processed_at": utcnow(), "pending_payload": None}
    async with Session() as s:
        res = await s.execute(update(_Obs).where(_Obs.id == obs_id, _Obs.status == PENDING).values(**values))
        await s.commit()
    return res.rowcount == 1


async def set_fields(obs_id: int, **fields: Any) -> None:
    async with Session() as s:
        await s.execute(update(_Obs).where(_Obs.id == obs_id).values(**fields))
        await s.commit()


async def undelivered(user_id: int, before: datetime) -> list[AttentionObservation]:
    async with Session() as s:
        rows = await s.scalars(
            select(_Obs)
            .where(
                _Obs.user_id == user_id,
                _Obs.status == DONE,
                _Obs.delivery == QUEUED,
                _Obs.processed_at < before,
            )
            .order_by(_Obs.id)
        )
        return list(rows)


async def users_needing_drain() -> list[int]:
    async with Session() as s:
        rows = await s.scalars(
            select(_Obs.user_id)
            .where((_Obs.status == PENDING) | (_Obs.delivery == QUEUED))
            .distinct()
            .order_by(_Obs.user_id)
        )
        return list(rows)


async def recent(
    user_id: int, since: datetime, *, origin: str | None = None, limit: int = 50
) -> list[AttentionObservation]:
    q = select(_Obs).where(_Obs.user_id == user_id, _Obs.status == DONE, _Obs.received_at >= since)
    if origin is not None:
        q = q.where(_Obs.origin == origin)
    async with Session() as s:
        return list(await s.scalars(q.order_by(_Obs.received_at.desc(), _Obs.id.desc()).limit(limit)))


async def by_ids(user_id: int, ids: Iterable[int]) -> list[AttentionObservation]:
    wanted = list(ids)
    if not wanted:
        return []
    async with Session() as s:
        rows = await s.scalars(
            select(_Obs).where(_Obs.user_id == user_id, _Obs.id.in_(wanted), _Obs.status == DONE)
        )
        found = {r.id: r for r in rows}
    return [found[i] for i in wanted if i in found]


async def has_any(user_id: int) -> bool:
    async with Session() as s:
        return await s.scalar(select(_Obs.id).where(_Obs.user_id == user_id).limit(1)) is not None


async def recent_debits(user_id: int, since: datetime, exclude_id: int) -> int:
    """Debits seen since `since` (burst detection). JSON is filtered in Python: portable, and tiny."""
    async with Session() as s:
        facts = await s.scalars(
            select(_Obs.facts)
            .where(
                _Obs.user_id == user_id, _Obs.status == DONE, _Obs.received_at >= since, _Obs.id != exclude_id
            )
            .order_by(_Obs.received_at.desc(), _Obs.id.desc())
            .limit(50)
        )
        return sum(1 for f in facts if ((f or {}).get("money") or {}).get("direction") == "debit")


async def add_pref(user_id: int, observation_id: int | None, kind: str, sentiment: str, summary: str) -> int:
    async with Session() as s:
        row = AttentionPref(
            user_id=user_id,
            observation_id=observation_id,
            kind=kind,
            sentiment=sentiment,
            summary=summary[:240],
            created_at=utcnow(),
        )
        s.add(row)
        await s.commit()
        return row.id


async def set_pref_point(pref_id: int, point_id: str) -> None:
    async with Session() as s:
        await s.execute(update(AttentionPref).where(AttentionPref.id == pref_id).values(point_id=point_id))
        await s.commit()


async def purge_before(cutoff: datetime) -> list[str]:
    """Delete observations created before `cutoff`; return their Qdrant point ids for deletion."""
    async with Session() as s:
        rows = (await s.execute(select(_Obs.id, _Obs.point_id).where(_Obs.created_at < cutoff))).all()
        if rows:
            await s.execute(delete(_Obs).where(_Obs.id.in_([r.id for r in rows])))
            await s.commit()
    return [r.point_id for r in rows if r.point_id]


async def expire_pending(cutoff: datetime) -> int:
    """Pending rows older than `cutoff` are closed unread: the snippet is dropped, nothing is sent."""
    async with Session() as s:
        res = await s.execute(
            update(_Obs)
            .where(_Obs.status == PENDING, _Obs.created_at < cutoff)
            .values(
                status=DONE,
                verdict="log",
                method="expired",
                summary="(not read in time)",
                pending_payload=None,
                processed_at=utcnow(),
            )
        )
        await s.commit()
    return int(res.rowcount or 0)
