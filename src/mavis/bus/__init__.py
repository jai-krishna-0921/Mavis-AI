"""Process-wide bus accessor: Redis Streams when REDIS_URL is set, otherwise in-process."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mavis.bus.base import BLOCK_MS, EventBus
from mavis.config import get_settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

SOCKET_TIMEOUT_S = BLOCK_MS / 1000 + 10

_bus: EventBus | None = None
_redis: Redis | None = None


def _make_redis(url: str) -> Redis:
    from redis.asyncio import Redis

    return Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=SOCKET_TIMEOUT_S,
        socket_connect_timeout=5,
        health_check_interval=30,
        retry_on_timeout=True,
    )


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        client = get_redis()
        if client is not None:
            from mavis.bus.redis_streams import RedisStreamsBus

            # the bus owns its own connection pool; get_redis() stays available for locks/health
            s = get_settings()
            _bus = RedisStreamsBus(_make_redis(s.redis_url), block_ms=BLOCK_MS,
                                   claim_idle_ms=s.bus_claim_idle_ms)
        else:
            from mavis.bus.inprocess import InProcessBus

            _bus = InProcessBus()
    return _bus


def set_bus(bus: EventBus | None) -> None:
    global _bus
    _bus = bus


def get_redis() -> Redis | None:
    """Shared Redis client for locks and health checks, or None in single-process mode."""
    global _redis
    url = get_settings().redis_url
    if not url:
        return None
    if _redis is None:
        _redis = _make_redis(url)
    return _redis
