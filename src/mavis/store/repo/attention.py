"""Attention observations and preferences (spec attention section 11)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from mavis.store.db import Session, utcnow
from mavis.store.models import AttentionObservation, AttentionPref, AttentionSender

PENDING, DONE = "pending", "done"
ORIGIN_LIVE, ORIGIN_BACKFILL = "live", "backfill"
QUEUED = "queued"
SOURCE_MAIL = "mail"
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
                _Obs.source == SOURCE_MAIL,  # mail redelivery never re-sends a Workspace row
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
            .where(_Obs.source == SOURCE_MAIL, (_Obs.status == PENDING) | (_Obs.delivery == QUEUED))
            .distinct()
            .order_by(_Obs.user_id)
        )
        return list(rows)


async def recent(
    user_id: int,
    since: datetime,
    *,
    origin: str | None = None,
    limit: int = 50,
    source: str | None = SOURCE_MAIL,
) -> list[AttentionObservation]:
    q = select(_Obs).where(_Obs.user_id == user_id, _Obs.status == DONE, _Obs.received_at >= since)
    if origin is not None:
        q = q.where(_Obs.origin == origin)
    if source is not None:  # mail readers (brief, evening wrap, digest) never see Workspace signals
        q = q.where(_Obs.source == source)
    async with Session() as s:
        return list(await s.scalars(q.order_by(_Obs.received_at.desc(), _Obs.id.desc()).limit(limit)))


async def by_ids(user_id: int, ids: Iterable[int]) -> list[AttentionObservation]:
    wanted = list(ids)
    if not wanted:
        return []
    async with Session() as s:
        rows = await s.scalars(
            select(_Obs).where(
                _Obs.user_id == user_id, _Obs.id.in_(wanted), _Obs.status == DONE, _Obs.source == SOURCE_MAIL
            )
        )
        found = {r.id: r for r in rows}
    return [found[i] for i in wanted if i in found]


async def has_any(user_id: int) -> bool:
    """The user has mail observations; Workspace signals do not make a mailbox "watched"."""
    async with Session() as s:
        q = select(_Obs.id).where(_Obs.user_id == user_id, _Obs.source == SOURCE_MAIL).limit(1)
        return await s.scalar(q) is not None


async def recent_debits(user_id: int, since: datetime, exclude_id: int) -> int:
    """Debits seen since `since` (burst detection). JSON is filtered in Python: portable, and tiny."""
    async with Session() as s:
        facts = await s.scalars(
            select(_Obs.facts)
            .where(
                _Obs.user_id == user_id,
                _Obs.source == SOURCE_MAIL,
                _Obs.status == DONE,
                _Obs.received_at >= since,
                _Obs.id != exclude_id,
            )
            .order_by(_Obs.received_at.desc(), _Obs.id.desc())
            .limit(50)
        )
        return sum(1 for f in facts if ((f or {}).get("money") or {}).get("direction") == "debit")


async def prior_security(user_id: int, sender_domain: str, exclude_id: int) -> bool:
    """This sender domain has sent authenticated SECURITY-kind mail before (urgency 5 gate, ruling I1).
    Only authenticated earlier notices count: a spoofed one cannot pave the way for a later one."""
    if not sender_domain:
        return False
    async with Session() as s:
        facts = await s.scalars(
            select(_Obs.facts)
            .where(
                _Obs.user_id == user_id,
                _Obs.source == SOURCE_MAIL,
                _Obs.status == DONE,
                _Obs.kind == "security",
                _Obs.sender_domain == sender_domain,
                _Obs.id != exclude_id,
            )
            .order_by(_Obs.id.desc())
            .limit(20)
        )
        return any((f or {}).get("authenticated") is True for f in facts)


async def baselined_on(user_id: int, counterparty_key: str, start: datetime, end: datetime) -> int:
    """Debits recorded into the money baseline for this payee among mail received in [start, end): the
    per-payee daily cap counts by the mail's day, so a backfill warms up each day separately."""
    async with Session() as s:
        facts = await s.scalars(
            select(_Obs.facts).where(
                _Obs.user_id == user_id,
                _Obs.source == SOURCE_MAIL,
                _Obs.status == DONE,
                _Obs.received_at >= start,
                _Obs.received_at < end,
            )
        )
        return sum(
            1
            for f in facts
            if (f or {}).get("baselined")
            and ((f or {}).get("money") or {}).get("counterparty_key") == counterparty_key
        )


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


async def purge_before(cutoff: datetime, user_id: int | None = None) -> list[str]:
    """Delete observations created before `cutoff` (one user's, or everyone's); return their Qdrant point
    ids for deletion."""
    q = select(_Obs.id, _Obs.point_id).where(_Obs.created_at < cutoff)
    if user_id is not None:
        q = q.where(_Obs.user_id == user_id)
    async with Session() as s:
        rows = (await s.execute(q)).all()
        if rows:
            await s.execute(delete(_Obs).where(_Obs.id.in_([r.id for r in rows])))
            await s.commit()
    return [r.point_id for r in rows if r.point_id]


async def expire_pending(cutoff: datetime, user_id: int | None = None) -> int:
    """Pending rows older than `cutoff` are closed unread: the snippet is dropped, nothing is sent."""
    where = [_Obs.status == PENDING, _Obs.created_at < cutoff]
    if user_id is not None:
        where.append(_Obs.user_id == user_id)
    async with Session() as s:
        res = await s.execute(
            update(_Obs)
            .where(*where)
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


async def insert_signal(
    user_id: int,
    message_id: str,
    *,
    source: str,
    kind: str,
    verdict: str,
    urgency: int,
    summary: str,
    facts: dict[str, Any],
    received_at: datetime,
) -> tuple[AttentionObservation, bool]:
    """A Workspace signal, decided by rules at once (no pending phase). Idempotent on (user, message_id):
    a webhook and a poll seeing the same share produce one row."""
    async with Session() as s:
        existing = await s.scalar(_by_message(user_id, message_id))
        if existing is not None:
            return existing, False
        now = utcnow()
        row = _Obs(
            user_id=user_id, message_id=message_id, thread_id="", origin=ORIGIN_LIVE, source=source,
            status=DONE, attempts=0, method="rules", sender_domain="", sender_name="", kind=kind[:24],
            needs_user=False, verdict=verdict, urgency=urgency, score=0.0, reasons=[], facts=facts,
            summary=summary[:240], action="", pending_payload=None, delivery="none", received_at=received_at,
            created_at=now, processed_at=now,
        )
        s.add(row)
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            again = await s.scalar(_by_message(user_id, message_id))
            assert again is not None
            return again, False
        await s.refresh(row)
        return row, True


async def signals(
    user_id: int, since: datetime, *, sources: Iterable[str], kinds: Iterable[str] | None = None,
    limit: int = 50,
) -> list[AttentionObservation]:
    q = select(_Obs).where(
        _Obs.user_id == user_id, _Obs.source.in_(list(sources)), _Obs.received_at >= since
    )
    if kinds is not None:
        q = q.where(_Obs.kind.in_(list(kinds)))
    async with Session() as s:
        return list(await s.scalars(q.order_by(_Obs.received_at.desc(), _Obs.id.desc()).limit(limit)))


async def sender_known(user_id: int, address: str) -> bool:
    """The address has mailed this user before (attention_senders): Workspace `actor_known`."""
    async with Session() as s:
        found = await s.scalar(
            select(AttentionSender.id).where(
                AttentionSender.user_id == user_id, AttentionSender.address == address.strip().lower()
            ).limit(1)
        )
    return found is not None
