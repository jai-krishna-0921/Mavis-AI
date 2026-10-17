# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Data access for notion_links: which Notion page backs which local thing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from mavis.domain import timeutil
from mavis.store.db import Session
from mavis.store.models import NotionLink


@dataclass(frozen=True)
class NotionLinkRow:
    id: int
    user_id: int
    kind: str
    local_key: str
    page_id: str
    pushed_hash: str
    pushed_at: datetime | None
    edited_at: datetime | None
    meta: dict = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _row(r: NotionLink) -> NotionLinkRow:
    return NotionLinkRow(
        id=r.id, user_id=r.user_id, kind=r.kind, local_key=r.local_key, page_id=r.page_id,
        pushed_hash=r.pushed_hash or "", pushed_at=timeutil.ensure_utc(r.pushed_at),
        edited_at=timeutil.ensure_utc(r.edited_at), meta=dict(r.meta or {}),
        created_at=timeutil.ensure_utc(r.created_at), updated_at=timeutil.ensure_utc(r.updated_at),
    )


async def get(user_id: int, kind: str, local_key: str) -> NotionLinkRow | None:
    async with Session() as s:
        r = await s.scalar(select(NotionLink).where(
            NotionLink.user_id == user_id, NotionLink.kind == kind, NotionLink.local_key == local_key))
        return _row(r) if r else None


async def by_page(user_id: int, page_id: str) -> NotionLinkRow | None:
    async with Session() as s:
        r = await s.scalar(select(NotionLink).where(
            NotionLink.user_id == user_id, NotionLink.page_id == page_id))
        return _row(r) if r else None


async def all_of(user_id: int, kind: str) -> list[NotionLinkRow]:
    async with Session() as s:
        rows = await s.scalars(select(NotionLink).where(
            NotionLink.user_id == user_id, NotionLink.kind == kind).order_by(NotionLink.id))
        return [_row(r) for r in rows]


async def upsert(user_id: int, kind: str, local_key: str, page_id: str, *, pushed_hash: str | None = None,
                 pushed_at: datetime | None = None, edited_at: datetime | None = None,
                 meta: dict | None = None) -> None:
    """Create or update the link. A field left as None keeps its stored value."""
    now = timeutil.now()
    async with Session() as s:
        r = await s.scalar(select(NotionLink).where(
            NotionLink.user_id == user_id, NotionLink.kind == kind, NotionLink.local_key == local_key))
        if r is None:
            r = NotionLink(user_id=user_id, kind=kind, local_key=local_key, page_id=page_id,
                           pushed_hash="", meta={}, created_at=now)
            s.add(r)
        r.page_id = page_id
        if pushed_hash is not None:
            r.pushed_hash = pushed_hash
        if pushed_at is not None:
            r.pushed_at = timeutil.ensure_utc(pushed_at)
        if edited_at is not None:
            r.edited_at = timeutil.ensure_utc(edited_at)
        if meta is not None:
            r.meta = dict(meta)
        r.updated_at = now
        await s.commit()


async def delete(user_id: int, kind: str, local_key: str) -> None:
    async with Session() as s:
        await s.execute(sa_delete(NotionLink).where(
            NotionLink.user_id == user_id, NotionLink.kind == kind, NotionLink.local_key == local_key))
        await s.commit()


async def delete_user_kinds(user_id: int, kinds: Iterable[str]) -> int:
    async with Session() as s:
        res = await s.execute(sa_delete(NotionLink).where(
            NotionLink.user_id == user_id, NotionLink.kind.in_(list(kinds))))
        await s.commit()
        return res.rowcount or 0



async def delete_all(user_id: int) -> int:
    async with Session() as s:
        res = await s.execute(sa_delete(NotionLink).where(NotionLink.user_id == user_id))
        await s.commit()
        return res.rowcount or 0
