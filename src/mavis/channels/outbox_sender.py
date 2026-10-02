"""Delivers outbox rows through the Channel with leasing, exponential backoff and rate-limit handling."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import structlog

from mavis.channels import get_channel
from mavis.channels.base import Channel, ChannelRateLimited
from mavis.store.db import utcnow
from mavis.store.models import OutboxMessage
from mavis.store.repo import outbox, users

log = structlog.get_logger(__name__)
MAX_ATTEMPTS = 8


class OutboxSender:
    def __init__(self, channel: Channel | None = None) -> None:
        self._channel = channel

    @property
    def channel(self) -> Channel:
        return self._channel or get_channel()

    async def run_once(self, now: datetime | None = None, limit: int = 20) -> int:
        now = now or utcnow()
        delivered = 0
        # due() holds back a user's later rows until the earlier one is sent, so re-query after each
        # pass to pick up the next bubble; stop when a pass delivers nothing (retry/backoff/empty).
        while delivered < limit:
            pass_delivered = 0
            for row in await outbox.due(now, limit - delivered):
                if not await outbox.claim(row.id, now):
                    continue  # another sender owns it
                if await self._attempt(row, now):
                    pass_delivered += 1
            if not pass_delivered:
                break
            delivered += pass_delivered
        return delivered

    async def _attempt(self, row: OutboxMessage, now: datetime) -> bool:
        try:
            await self._deliver(row)
        except ChannelRateLimited as exc:
            await outbox.mark_retry(row.id, "rate limited", now + timedelta(seconds=exc.retry_after),
                                    count_attempt=False)
        except Exception as exc:  # noqa: BLE001
            attempts = row.attempts + 1
            log.warning("outbox.delivery_failed", outbox_id=row.id, attempt=attempts, error=repr(exc))
            if attempts >= MAX_ATTEMPTS:
                await outbox.mark_failed(row.id, repr(exc)[:500])
            else:
                await outbox.mark_retry(row.id, repr(exc)[:500],
                                        now + timedelta(seconds=min(2**attempts, 300)))
        else:
            return True
        return False

    async def _deliver(self, row: OutboxMessage) -> None:
        user = await users.get(row.user_id)
        if user.telegram_chat_id is None:
            raise RuntimeError(f"user {row.user_id} has no chat id")
        msg = outbox.to_outbound(row)
        ids: list[int] = []
        if msg.document_path:
            ids.append(await self.channel.send_document(user.telegram_chat_id, msg.document_path, msg.text))
        elif msg.text:
            ids += await self.channel.send_text(user.telegram_chat_id, msg.text, msg.buttons or None)
        await outbox.mark_sent(row.id, ids)

    async def run_forever(self, interval_s: float = 0.3) -> None:
        while True:
            try:
                delivered = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox.loop_error")
                delivered = 0
            if delivered == 0:
                await asyncio.sleep(interval_s)


async def deliver_pending(channel: Channel | None = None, limit: int = 50) -> int:
    """Deliver everything due right now (used by tests and the local chat REPL)."""
    return await OutboxSender(channel).run_once(limit=limit)
