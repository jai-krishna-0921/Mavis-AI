"""Telegram send pacing (shared contract A with plan 11).

A global per-second window across every process (Redis INCR on the current second, in-memory without
Redis). `reserve()` returns how long the caller should wait before sending; 0.0 means send now. Callers
that get a wait do not send this pass (the outbox) or sleep it (the card editor). Plan 11 adds per-chat
buckets and the broadcast cap behind the same `reserve(chat_id, kind=...)` signature."""

from __future__ import annotations

import time

from mavis.bus import get_redis
from mavis.config import get_settings


class SendPacer:
    def __init__(self, rate: float | None = None) -> None:
        self.rate = float(rate if rate is not None else get_settings().telegram_global_send_rate)
        self._window = -1
        self._count = 0

    async def reserve(self, chat_id: int | None = None, *, kind: str = "chat") -> float:
        now = time.time()
        second = int(now)
        limit = max(1, int(self.rate))
        client = get_redis()
        if client is not None:
            key = f"mavis:pace:global:{second}"
            count = int(await client.incr(key))
            if count == 1:
                await client.expire(key, 2)
        else:
            if second != self._window:
                self._window, self._count = second, 0
            self._count += 1
            count = self._count
        if count <= limit:
            return 0.0
        return max(0.01, (second + 1) - now)


_pacer: SendPacer | None = None


def get_pacer() -> SendPacer:
    global _pacer
    if _pacer is None:
        _pacer = SendPacer()
    return _pacer


def set_pacer(p: SendPacer | None) -> None:
    global _pacer
    _pacer = p
