"""Long-polling for dev (no public URL needed). Feeds the same normaliser as the webhook."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from telegram import Bot

from zento.bus.base import EventBus
from zento.channels.telegram_updates import ingest_update

log = structlog.get_logger(__name__)
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]


async def run_polling(bus: EventBus, token: str, bot: Any | None = None) -> None:
    bot = bot or Bot(token)

    # Startup with retry loop for transient network errors
    backoff = 1.0
    while True:
        try:
            await bot.initialize()
            await bot.delete_webhook(drop_pending_updates=False)
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram.startup_failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    offset: int | None = None
    backoff = 1.0
    log.info("telegram.polling_started")
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=25, allowed_updates=ALLOWED_UPDATES)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram.get_updates_failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        for upd in updates:
            try:
                await ingest_update(upd.to_dict(), bus)
            except Exception:
                # keep the offset at this update so the next poll re-fetches it; publishing is deduped
                log.exception("telegram.ingest_failed", update_id=upd.update_id)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                break
            offset = upd.update_id + 1
