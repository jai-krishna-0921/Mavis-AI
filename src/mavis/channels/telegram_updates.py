"""Raw Telegram Update dict -> Event. Shared by the webhook route and the dev poller."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from mavis.access.codes import display, find_code, looks_like_code
from mavis.access.inbound import get_inbound_limiter
from mavis.bus.base import EventBus
from mavis.channels.test_sink import FIXTURE_PREFIX, MIRROR_HEADER, active_test_chat, is_test_chat
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


def _gate_in_worker() -> bool:
    return get_settings().access_mode in ("shadow", "invite")


def _chat_allowed(chat_id: int) -> bool:
    """allowlist mode (and shadow, which still enforces the list): owner chats plus the test chats.
    Empty list = allow-all in dev only (warn once); every other env denies."""
    global _warned_open_allowlist
    s = get_settings()
    if is_test_chat(chat_id, s):
        return True  # the live E2E test user (synthetic id, webhook + secret only); sends go to the sink
    if s.owner_telegram_chat_ids:
        return chat_id in s.owner_telegram_chat_ids
    if s.env != "dev":
        return False
    if not _warned_open_allowlist:
        _warned_open_allowlist = True
        log.warning("telegram.allowlist_empty_allowing_all", hint="set OWNER_TELEGRAM_CHAT_IDS")
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
    msg: dict[str, Any] | None = None

    if member := data.get("my_chat_member"):
        chat = member.get("chat") or {}
        if chat.get("id") is None or not _gate_in_worker():
            return False  # allowlist mode keeps today's behaviour: membership changes are ignored
        status = str((member.get("new_chat_member") or {}).get("status", ""))
        return await bus.publish(Event(
            id=f"tg:update:{update_id}", user_id=0, type=EventType.CHAT_MEMBER, occurred_at=utcnow(),
            source="telegram", trust=Trust.SYSTEM,
            payload={"chat_id": chat["id"], "chat_type": chat.get("type", ""), "status": status}))

    chat_type = "private"
    if cq := data.get("callback_query"):
        await _answer(answer, cq.get("id"))
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        chat_type = (message.get("chat") or {}).get("type", "private")
        sender = cq.get("from") or {}
        event_type = EventType.BUTTON_PRESSED
        payload: dict[str, Any] = {"data": cq.get("data", ""), "callback_query_id": cq.get("id"),
                                   "message_id": message.get("message_id")}
        occurred = utcnow()
    elif msg := data.get("message") or data.get("edited_message"):
        chat_id = (msg.get("chat") or {}).get("id")
        chat_type = (msg.get("chat") or {}).get("type", "private")
        sender = msg.get("from") or {}
        event_type = EventType.USER_MESSAGE
        text = msg.get("text") or msg.get("caption") or ""
        payload = {"text": text, "message_id": msg.get("message_id")}
        if replied := (msg.get("reply_to_message") or {}).get("text"):
            payload["reply_to_text"] = str(replied)[:4000]  # the message they answered (card edits need it)
        if m := _COMMAND.match(text):
            payload["command"] = m.group(1).lower()
        if file := _file(msg):
            payload["file"] = file.model_dump()
        if isinstance(loc := msg.get("location"), dict) and "latitude" in loc and "longitude" in loc:
            payload["location"] = {"latitude": loc["latitude"], "longitude": loc["longitude"]}
        occurred = datetime.fromtimestamp(msg["date"], UTC) if msg.get("date") else utcnow()
    else:
        return False

    if chat_id is None:
        return False
    s = get_settings()
    if _gate_in_worker() and chat_type != "private" and not is_test_chat(chat_id, s):
        log.info("telegram.non_private_dropped", chat_type=chat_type)
        return False
    if s.access_mode != "invite" and not _chat_allowed(chat_id):
        # the api role never sends, so no refusal reply: the owner reads chat_id from this log line
        log.warning("telegram.chat_not_allowed", chat_id=chat_id)
        return False
    file_info = payload.get("file")
    if file_info and str(file_info.get("file_id", "")).startswith(FIXTURE_PREFIX) \
            and chat_id != active_test_chat(get_settings()):
        payload.pop("file")  # only the harness (webhook + secret, synthetic chat) may name a fixture
    reply = (msg or {}).get("reply_to_message") if event_type is EventType.USER_MESSAGE else None
    if (reply and (reply.get("from") or {}).get("is_bot")
            and str(reply.get("text", "")).startswith(MIRROR_HEADER)):
        log.info("telegram.mirror_reply_ignored", chat_id=chat_id)  # the owner answering a [test] copy
        return False

    user, _ = await users.get_or_create_by_chat(chat_id, sender.get("first_name"),
                                                telegram_user_id=sender.get("id"))
    # The owner is grandfathered: a listed owner chat is never "pending", whatever its row says yet.
    pending = (user.status != "active" and not is_test_chat(chat_id, s)
               and chat_id not in s.owner_telegram_chat_ids)
    owner = chat_id in s.owner_telegram_chat_ids
    if not owner and (s.access_mode == "invite" or (s.access_mode == "shadow" and pending)):
        if not await get_inbound_limiter().allow(chat_id, pending=pending):
            minute = int(occurred.timestamp() // 60)
            return await bus.publish(Event(
                id=f"tg:ratelimited:{chat_id}:{minute}", user_id=user.id, type=EventType.RATE_LIMITED,
                occurred_at=occurred, source="telegram", trust=Trust.SYSTEM, payload={}))
    if pending and s.access_mode == "invite":
        # Spec 4.2: no message content is stored or carried for pending users, only what the gate needs.
        text = str(payload.get("text", ""))
        keep = text.startswith("/start") or looks_like_code(text)
        if keep and not text.startswith("/start") and (embedded := find_code(text)) is not None:
            text = display(embedded)  # a code inside a sentence: carry the code only, never the sentence
        payload = {"text": text if keep else "", "message_id": payload.get("message_id"), "pending": True}
        if keep and (m := _COMMAND.match(text)):
            payload["command"] = m.group(1).lower()
    event = Event(id=f"tg:update:{update_id}", user_id=user.id, type=event_type, occurred_at=occurred,
                  source="telegram", payload=payload, trust=Trust.USER)
    return await bus.publish(event)
