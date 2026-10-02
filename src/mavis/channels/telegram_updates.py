"""Raw Telegram Update dict -> Event. Shared by the webhook route and the dev poller."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from mavis.bus.base import EventBus
from mavis.config import get_settings
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.messages import InboundFile
from mavis.store.db import utcnow
from mavis.store.repo import users

log = structlog.get_logger(__name__)
_COMMAND = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@\w+)?(?:\s|$)")


def _file(msg: dict[str, Any]) -> InboundFile | None:
    if doc := msg.get("document"):
        return InboundFile(file_id=doc["file_id"], file_name=doc.get("file_name") or "document",
                           mime_type=doc.get("mime_type"), size=doc.get("file_size"))
    if photos := msg.get("photo"):
        best = max(photos, key=lambda p: p.get("width", 0) * p.get("height", 0))
        return InboundFile(file_id=best["file_id"], file_name=f"photo_{best.get('file_unique_id', 'x')}.jpg",
                           mime_type="image/jpeg", size=best.get("file_size"))
    return None


_warned_open_allowlist = False


def _chat_allowed(chat_id: int) -> bool:
    """Empty allowlist = allow-all in dev only (warn once); every other env denies."""
    global _warned_open_allowlist
    s = get_settings()
    if s.allowed_telegram_chat_ids:
        return chat_id in s.allowed_telegram_chat_ids
    if s.env != "dev":
        return False
    if not _warned_open_allowlist:
        _warned_open_allowlist = True
        log.warning("telegram.allowlist_empty_allowing_all", hint="set ALLOWED_TELEGRAM_CHAT_IDS")
    return True


async def ingest_update(data: dict[str, Any], bus: EventBus) -> bool:
    """Normalise and publish one update. Returns True if a new event was published."""
    update_id = data.get("update_id")
    if update_id is None:
        return False

    if cq := data.get("callback_query"):
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        sender = cq.get("from") or {}
        event_type = EventType.BUTTON_PRESSED
        payload: dict[str, Any] = {"data": cq.get("data", ""), "callback_query_id": cq.get("id"),
                                   "message_id": message.get("message_id")}
        occurred = utcnow()
    elif msg := data.get("message") or data.get("edited_message"):
        chat_id = (msg.get("chat") or {}).get("id")
        sender = msg.get("from") or {}
        event_type = EventType.USER_MESSAGE
        text = msg.get("text") or msg.get("caption") or ""
        payload = {"text": text, "message_id": msg.get("message_id")}
        if m := _COMMAND.match(text):
            payload["command"] = m.group(1).lower()
        if file := _file(msg):
            payload["file"] = file.model_dump()
        occurred = datetime.fromtimestamp(msg.get("date") or time.time(), UTC)
    else:
        return False

    if chat_id is None:
        return False
    if not _chat_allowed(chat_id):
        # the api role never sends, so no refusal reply: the owner reads chat_id from this log line
        log.warning("telegram.chat_not_allowed", chat_id=chat_id)
        return False

    user, _ = await users.get_or_create_by_chat(chat_id, sender.get("first_name"))
    event = Event(id=f"tg:update:{update_id}", user_id=user.id, type=event_type, occurred_at=occurred,
                  source="telegram", payload=payload, trust=Trust.USER)
    return await bus.publish(event)
