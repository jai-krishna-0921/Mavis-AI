"""Fixed-window per-IP rate limit for public webhook routes. Redis when available, else in-memory."""

from __future__ import annotations

import time

from fastapi import HTTPException, Request, status

from mavis.bus import get_redis

LIMIT = 120
WINDOW_S = 60


class RateLimiter:
    def __init__(self, limit: int = LIMIT, window_s: int = WINDOW_S) -> None:
        self.limit = limit
        self.window_s = window_s
        self._mem: dict[str, tuple[int, int]] = {}  # key -> (window_id, count)

    async def hit(self, key: str) -> bool:
        """True if the request is allowed."""
        window = int(time.time() // self.window_s)
        client = get_redis()
        if client is not None:
            rkey = f"mavis:rl:{key}:{window}"
            try:
                count = await client.incr(rkey)
                if count == 1:
                    await client.expire(rkey, self.window_s * 2)
                return count <= self.limit
            except Exception:  # noqa: BLE001 - fall back to in-memory if redis blips
                pass
        prev_window, count = self._mem.get(key, (window, 0))
        count = count + 1 if prev_window == window else 1
        self._mem[key] = (window, count)
        if len(self._mem) > 10_000:
            self._mem = {k: v for k, v in self._mem.items() if v[0] == window}
        return count <= self.limit


_limiter: RateLimiter | None = None


def _get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def client_ip(request: Request) -> str:
    """The api is only reachable through Caddy in prod, so X-Forwarded-For is trustworthy there."""
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


async def webhook_rate_limit(request: Request) -> None:
    if not await _get_limiter().hit(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limited")


_buckets: dict[tuple[str, int, int], RateLimiter] = {}


async def hit(bucket: str, key: str, limit: int, window_s: int = WINDOW_S) -> bool:
    """True if `key` is within `limit` hits per `window_s` in the named bucket (own counters per bucket)."""
    limiter = _buckets.setdefault((bucket, limit, window_s), RateLimiter(limit, window_s))
    return await limiter.hit(f"{bucket}:{key}")
