"""Note the ids of messages a channel adapter sent, for /clear. Telegram adapters call this after each send.
Best effort: never raises, so a bookkeeping failure cannot turn a delivered message into a retry."""

from __future__ import annotations

from collections.abc import Iterable

import structlog

log = structlog.get_logger(__name__)


async def note_sent(chat_id: object, message_ids: Iterable[int]) -> None:
    if not isinstance(chat_id, int):
        return
    try:
        from mavis.store.repo import chat_ids

        await chat_ids.record(chat_id, message_ids, "out")
    except Exception as exc:  # noqa: BLE001
        log.debug("sent_log.failed", error=type(exc).__name__)
