from __future__ import annotations

import pytest

from mavis.access.inbound import InboundLimiter


@pytest.mark.parametrize("chat", [5001, 7302, 9944])
async def test_memory_bucket_allows_burst_then_refills(settings, chat):
    now = [1000.0]
    lim = InboundLimiter(clock=lambda: now[0])
    allowed = [await lim.allow(chat, pending=False) for _ in range(12)]
    assert allowed.count(True) == settings.inbound_burst
    now[0] += 3.0  # 20/min = one token per 3 s
    assert await lim.allow(chat, pending=False)


async def test_pending_bucket_is_tighter_and_per_chat(settings):
    now = [0.0]
    lim = InboundLimiter(clock=lambda: now[0])
    assert [await lim.allow(1, pending=True) for _ in range(5)].count(True) == settings.inbound_pending_burst
    assert await lim.allow(2, pending=True)  # another chat has its own bucket


async def test_redis_bucket_matches_memory(fake_redis, settings):
    now = [50.0]
    lim = InboundLimiter(clock=lambda: now[0])
    got = [await lim.allow(42, pending=False) for _ in range(11)]
    assert got.count(True) == settings.inbound_burst
    assert await fake_redis.exists("mavis:inbound:c42")
