"""Per-chat inbound token bucket at intake (spec 9.1). Redis (one Lua call) when available, else memory."""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog

from mavis import bus
from mavis.config import get_settings

log = structlog.get_logger(__name__)

# KEYS[1] bucket hash; ARGV: now_s, rate_per_s, burst, cost. Returns 1 if allowed.
BUCKET_LUA = """
local t = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local now = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local burst = tonumber(ARGV[3])
local tokens = tonumber(t[1]) or burst; local ts = tonumber(t[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate)
local ok = 0
if tokens >= tonumber(ARGV[4]) then tokens = tokens - tonumber(ARGV[4]); ok = 1 end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(burst / rate) + 60)
return ok
"""


class InboundLimiter:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._mem: dict[tuple[int, bool], tuple[float, float]] = {}
        self._script = None
        self._script_client = None

    def _params(self, pending: bool) -> tuple[float, int]:
        s = get_settings()
        if pending:
            return s.inbound_pending_rate_per_min / 60.0, s.inbound_pending_burst
        return s.inbound_rate_per_min / 60.0, s.inbound_burst

    async def allow(self, chat_id: int, *, pending: bool) -> bool:
        rate, burst = self._params(pending)
        now = self._clock()
        client = bus.get_redis()
        if client is not None:
            try:
                if self._script is None or self._script_client is not client:
                    self._script, self._script_client = client.register_script(BUCKET_LUA), client
                key = f"mavis:inbound:{'p' if pending else 'c'}{chat_id}"
                return bool(await self._script(keys=[key], args=[now, rate, burst, 1]))
            except Exception as exc:  # noqa: BLE001 - a Redis blip must not drop real users
                log.warning("inbound.redis_failed", error=type(exc).__name__)
        k = (chat_id, pending)
        tokens, ts = self._mem.get(k, (float(burst), now))
        tokens = min(float(burst), tokens + (now - ts) * rate)
        ok = tokens >= 1
        self._mem[k] = (tokens - 1 if ok else tokens, now)
        if len(self._mem) > 50_000:
            self._mem.clear()
        return ok


_limiter: InboundLimiter | None = None


def get_inbound_limiter() -> InboundLimiter:
    global _limiter
    if _limiter is None:
        _limiter = InboundLimiter()
    return _limiter


def set_inbound_limiter(lim: InboundLimiter | None) -> None:
    global _limiter
    _limiter = lim
