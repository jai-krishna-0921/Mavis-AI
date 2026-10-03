"""Persisted initiative decisions, keyed by event id, so a retried event reuses its first decision."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision
from mavis.store.db import Session
from mavis.store.models import InitiativeDecisionRow

KEEP_FOR = timedelta(days=7)  # far longer than any retry window


async def get(event_id: str) -> InitiativeDecision | None:
    async with Session() as s:
        row = await s.scalar(select(InitiativeDecisionRow).where(InitiativeDecisionRow.event_id == event_id))
        return InitiativeDecision.model_validate(row.decision) if row else None


async def save(user_id: int, event_id: str, decision: InitiativeDecision) -> None:
    now = timeutil.now()
    async with Session() as s:
        cutoff = now - KEEP_FOR
        await s.execute(delete(InitiativeDecisionRow).where(InitiativeDecisionRow.created_at < cutoff))
        s.add(InitiativeDecisionRow(event_id=event_id, user_id=user_id,
                                    decision=decision.model_dump(mode="json"), created_at=now))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()  # a concurrent attempt saved first; either decision is fine
