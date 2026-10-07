"""Raw Telegram Update dict -> Event. Shared by the webhook route and the dev poller."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
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
    if s.test_telegram_chat_id is not None and chat_id == s.test_telegram_chat_id:
        return True  # the live E2E test user; its sends go to the log sink (channels.test_sink)
    if s.allowed_telegram_chat_ids:
        return chat_id in s.allowed_telegram_chat_ids
    if s.env != "dev":
        return False
    if not _warned_open_allowlist:
        _warned_open_allowlist = True
        log.warning("telegram.allowlist_empty_allowing_all", hint="set ALLOWED_TELEGRAM_CHAT_IDS")
    return True


Answerer = Callable[[str], Awaitable[Any]]
_default_bot: Any | None = None


async def _default_answer(callback_query_id: str) -> None:
    global _default_bot
    token = get_settings().telegram_bot_token
    if not token:
        return
    if _default_bot is None:
        from telegram import Bot

        _default_bot = Bot(token)
    await _default_bot.answer_callback_query(callback_query_id)


async def _answer(answer: Answerer | None, callback_query_id: str | None) -> None:
    """Stop the button spinner right away. Best effort: it must never block or fail ingestion."""
    if not callback_query_id:
        return
    try:
        await (answer or _default_answer)(callback_query_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram.answer_callback_failed", error=type(exc).__name__)


async def ingest_update(data: dict[str, Any], bus: EventBus, answer: Answerer | None = None) -> bool:
    """Normalise and publish one update. Returns True if a new event was published.

    `answer(callback_query_id)` acknowledges button taps; the webhook path defaults to a Bot built from
    the configured token, the poller passes its own bot's method.
    """
    update_id = data.get("update_id")
    if update_id is None:
        return False

    if cq := data.get("callback_query"):
        await _answer(answer, cq.get("id"))
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
        occurred = datetime.fromtimestamp(msg["date"], UTC) if msg.get("date") else utcnow()
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
