"""Test helper: outbox texts by dedupe prefix across users, for one-user tests (production scopes by user)."""

from __future__ import annotations

from sqlalchemy import select

from mavis.store.db import Session
from mavis.store.models import OutboxMessage


async def texts(prefix: str) -> list[str]:
    async with Session() as s:
        rows = await s.scalars(select(OutboxMessage.text)
                               .where(OutboxMessage.dedupe_key.startswith(prefix, autoescape=True))
                               .order_by(OutboxMessage.id))
        return list(rows)
