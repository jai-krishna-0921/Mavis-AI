# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Process liveness for the outside uptime check: worker and timer each refresh a short-lived Redis key.

A process that crashed, hung or lost Redis stops refreshing, the key expires, and /status reports degraded.
The container healthchecks only see that the process exists; this sees that its event loop still runs.
"""

from __future__ import annotations

import asyncio

import structlog

from mavis.bus import get_redis

log = structlog.get_logger()

ROLES = ("worker", "timer")
INTERVAL_S = 30
TTL_S = 120  # four missed beats: a slow loop or a Redis blip is not an outage


def key(role: str) -> str:
    return f"mavis:alive:{role}"


async def beat_forever(role: str) -> None:
    """Refresh this process's key until cancelled. No Redis (single-process dev): nothing to report."""
    redis = get_redis()
    if redis is None:
        return
    while True:
        try:
            await redis.set(key(role), "1", ex=TTL_S)
        except Exception:  # noqa: BLE001 - a missed beat is what the check is for; keep the process running
            log.warning("liveness.beat_failed", role=role)
        await asyncio.sleep(INTERVAL_S)


async def missing() -> list[str]:
    """Roles whose key has expired (all of them when Redis is unreachable)."""
    redis = get_redis()
    if redis is None:
        return []
    try:
        return [r for r in ROLES if not await redis.exists(key(r))]
    except Exception:  # noqa: BLE001 - unreachable Redis means nothing can be vouched for
        return list(ROLES)
