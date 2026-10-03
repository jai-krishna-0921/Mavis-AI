"""Shared builders for attention tests. Fake domains only (spec: no sender-specific rules)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mavis.domain.events import Event
from mavis.tools.integrations.normalize import email_event


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
