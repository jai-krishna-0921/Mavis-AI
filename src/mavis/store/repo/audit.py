"""Append-only audit trail of approvals and side-effecting tool calls."""

from __future__ import annotations

from sqlalchemy import select

from mavis.store.db import Session
from mavis.store.models import AuditLog


async def record(user_id: int, actor: str, action: str, detail: dict) -> None:
    async with Session() as s:
        s.add(AuditLog(user_id=user_id, actor=actor, action=action, detail=detail))
        await s.commit()


async def recent(user_id: int, limit: int = 20) -> list[AuditLog]:
    async with Session() as s:
        rows = await s.scalars(
            select(AuditLog).where(AuditLog.user_id == user_id).order_by(AuditLog.id.desc()).limit(limit)
        )
        return list(rows)
