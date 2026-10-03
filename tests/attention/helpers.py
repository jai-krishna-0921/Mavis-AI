"""Shared builders for attention tests. Fake domains only (spec: no sender-specific rules)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from mavis.domain.events import Event
from mavis.store.db import Session
from mavis.store.models import OutboxMessage
from mavis.tools.integrations.normalize import email_event
from mavis.worker.runner import handle_event, handle_job

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def raw_email(
    mid: str,
    *,
    sender: str = "Someone <someone@example.com>",
    subject: str = "",
    text: str = "",
    labels: tuple[str, ...] = ("INBOX",),
    unsubscribe: bool = False,
    at: datetime | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "messageId": mid,
        "threadId": f"t-{mid}",
        "sender": sender,
        "subject": subject,
        "messageText": text,
        "labelIds": list(labels),
    }
    if at is not None:
        d["messageTimestamp"] = at.isoformat()
    if unsubscribe:
        d["payload"] = {"headers": [{"name": "List-Unsubscribe", "value": "<mailto:u@list.example>"}]}
    return d


def email(user_id: int, mid: str, **kw: Any) -> Event:
    event = email_event(user_id, raw_email(mid, **kw), source="poller")
    assert event is not None
    return event


def pending_payload(event: Event) -> dict[str, Any]:
    keys = (
        "from",
        "from_address",
        "from_name",
        "subject",
        "snippet",
        "labels",
        "list_unsubscribe",
        "received_at",
    )
    return {k: event.payload.get(k) for k in keys}


async def outbox_rows() -> list[OutboxMessage]:
    async with Session() as s:
        return list(await s.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))


async def outbox_texts() -> list[str]:
    return [r.text for r in await outbox_rows()]


@asynccontextmanager
async def running(bus):
    """The worker's event and job consumers on an InProcessBus, running until the bus is idle."""
    tasks = [
        asyncio.create_task(bus.consume_events("workers", "t", handle_event)),
        asyncio.create_task(bus.consume_jobs("workers", "t", handle_job)),
    ]
    try:
        yield
        await asyncio.wait_for(bus.wait_idle(), timeout=20)
    finally:
        for t in tasks:
            t.cancel()
