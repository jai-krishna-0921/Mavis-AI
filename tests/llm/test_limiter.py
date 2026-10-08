"""Shared limiter: one Redis, several limiter instances (processes). Pro plan defaults: 3 slots, bg 1."""

from __future__ import annotations

import asyncio

import pytest

from mavis.domain.errors import LLMError
from mavis.llm.context import bind_user
from mavis.llm.limiter import SharedLimiter


def _lim(client, **kw) -> SharedLimiter:
    args = {"provider": "primary", "slots": 3, "bg_max": 1, "best_effort_max": 0, "user_max": 2,
            "hold_ttl_s": 5.0}
    return SharedLimiter(client, **{**args, **kw})


async def test_two_processes_share_the_global_cap(fake_redis, settings):
    a, b = _lim(fake_redis), _lim(fake_redis)
    held = []
    for i, lim in enumerate([a, b, a]):
        with bind_user(100 + i, "chat"):
            held.append(await lim.acquire("interactive", 1.0))
    with bind_user(200, "chat"), pytest.raises(LLMError):
        await b.acquire("interactive", 0.3)
    a.release(held[0])
    await asyncio.sleep(0.05)
    with bind_user(201, "chat"):
        assert await b.acquire("interactive", 1.0) is not None


async def test_background_lane_cap_keeps_chat_slots(fake_redis, settings):
    lim = _lim(fake_redis)
    with bind_user(1, "task"):
        bg = await lim.acquire("background", 1.0)
    with bind_user(2, "task"), pytest.raises(LLMError):
        await lim.acquire("background", 0.3)  # bg_max = 1
    for uid in (3, 4):
        with bind_user(uid, "chat"):
            assert await lim.acquire("interactive", 1.0) is not None
    lim.release(bg)


async def test_best_effort_never_runs_on_pro_defaults(fake_redis, settings):
    lim = _lim(fake_redis)
    with bind_user(9, "memory"), pytest.raises(LLMError):
        await lim.acquire("best_effort", 0.5)


@pytest.mark.parametrize("busy_user,other_user", [(5, 6), (41, 42), (900, 7)])
async def test_fewest_held_user_wins_the_next_slot(fake_redis, settings, busy_user, other_user):
    lim = _lim(fake_redis, slots=2, bg_max=2)
    with bind_user(busy_user, "chat"):
        l1 = await lim.acquire("interactive", 1.0)
        l2 = await lim.acquire("interactive", 1.0)
    order: list[int] = []

    async def want(uid: int) -> None:
        with bind_user(uid, "chat"):
            lease = await lim.acquire("interactive", 3.0)
        order.append(uid)
        lim.release(lease)

    t_busy = asyncio.create_task(want(busy_user))
    await asyncio.sleep(0.05)
    t_other = asyncio.create_task(want(other_user))
    await asyncio.sleep(0.05)
    lim.release(l1)
    await asyncio.gather(t_busy, t_other)
    lim.release(l2)
    assert order[0] == other_user  # the user holding fewer slots goes first, though it queued later


async def test_dead_holder_slot_expires(fake_redis, settings):
    lim = _lim(fake_redis, slots=1, hold_ttl_s=0.3)
    with bind_user(1, "chat"):
        await lim.acquire("interactive", 1.0)  # never released: the process "died"
    with bind_user(2, "chat"):
        assert await lim.acquire("interactive", 2.0) is not None


async def test_shared_backoff_is_seen_by_every_process(fake_redis, settings):
    from mavis.llm.limiter import SharedProviderState

    a, b = SharedProviderState(fake_redis, "primary"), SharedProviderState(fake_redis, "primary")
    await a.note_rate_limit_async(None)
    await b.refresh()
    assert b.unavailable_s() > 0


async def test_redis_down_falls_back_to_local_limiter(settings, monkeypatch, caplog):
    from mavis.config import get_settings
    from mavis.llm import limiter

    monkeypatch.setenv("LLM_LIMITER", "redis")
    get_settings.cache_clear()

    class _Down:
        def register_script(self, _src):
            async def boom(**_kw):
                raise ConnectionError("down")
            return boom

    monkeypatch.setattr(limiter.bus, "get_redis", lambda: _Down())
    backend = limiter.get_limiter(secondary=False)
    lease = await backend.acquire("interactive", 1.0)
    backend.release(lease)
    assert isinstance(backend, limiter.FallingBackLimiter)


async def test_one_user_cannot_take_every_slot_while_others_wait(fake_redis, settings):
    lim = _lim(fake_redis, slots=3, user_max=2)
    got = []
    for _ in range(2):
        with bind_user(7, "chat"):
            got.append(await lim.acquire("interactive", 1.0))

    async def other() -> object:
        with bind_user(8, "chat"):
            return await lim.acquire("interactive", 2.0)

    t = asyncio.create_task(other())
    await asyncio.sleep(0.05)
    with bind_user(7, "chat"), pytest.raises(LLMError):
        await lim.acquire("interactive", 0.3)  # a third for user 7 loses to the waiting user 8
    assert await t is not None


async def test_a_waiter_that_stops_polling_does_not_block_the_queue(fake_redis, settings):
    lim = _lim(fake_redis, slots=1)
    with bind_user(1, "chat"):
        held = await lim.acquire("interactive", 1.0)
    # a ghost waiter from a dead process: queued first, never polls again
    await fake_redis.zadd("mavis:llm:primary:queue", {"ghost": 1})
    await fake_redis.hset("mavis:llm:primary:waiter:ghost", mapping={"uid": 99, "rank": 0, "since": 1})
    await fake_redis.pexpire("mavis:llm:primary:waiter:ghost", 100)
    lim.release(held)
    with bind_user(2, "chat"):
        assert await lim.acquire("interactive", 3.0) is not None


async def test_released_leases_free_the_slot_for_the_same_process(fake_redis, settings):
    lim = _lim(fake_redis, slots=1)
    for uid in (1, 2, 3):
        with bind_user(uid, "chat"):
            lease = await lim.acquire("interactive", 1.0)
        lim.release(lease)
        await asyncio.sleep(0.02)
    assert await fake_redis.zcard("mavis:llm:primary:holders") == 0


async def test_call_uses_the_shared_limiter_and_shares_a_429_backoff(fake_redis, settings, monkeypatch):
    import openai

    from mavis.config import get_settings
    from mavis.llm import limiter, models

    monkeypatch.setenv("LLM_LIMITER", "redis")
    get_settings.cache_clear()
    limiter.reset_limiters()
    models._ollama.reset()

    async def op():
        raise openai.RateLimitError("slow down", response=_resp(429, {"retry-after": "7"}), body=None)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(models, "_sleep", no_sleep)
    with bind_user(5, "chat"), pytest.raises(openai.RateLimitError):  # secondary exists: no retry
        await models._call(op, "interactive", models._Deadline(30), may_fail_over=True)
    await asyncio.sleep(0.05)  # the Redis write is fire and forget
    assert await fake_redis.zcard("mavis:llm:primary:holders") == 0
    assert await fake_redis.get("mavis:llm:primary:backoff_until") is not None
    # another process (fresh local state) learns of the backoff before its first attempt
    models._ollama.reset()
    await models._refresh_shared_state()
    assert models._ollama.backoff_remaining() > 3
    models._ollama.reset()


def _resp(status: int, headers: dict):
    import httpx

    return httpx.Response(status, headers=headers, request=httpx.Request("POST", "https://x"))


@pytest.mark.parametrize("priority,wait,secondary,expected", [
    ("interactive", 20.0, True, True), ("interactive", 1.0, True, False),
    ("interactive", 20.0, False, False), ("best_effort", 99.0, True, False),
    ("background", 0.0, True, False)])
def test_overflow_routing(settings, monkeypatch, priority, wait, secondary, expected):
    from mavis.llm import models

    class _Backend:
        def estimate_wait_s(self):
            return wait

    models._ollama.reset()
    monkeypatch.setattr(models, "_secondary_ready", lambda tier: secondary)
    monkeypatch.setattr(models, "get_limiter", lambda sec=False: _Backend())
    assert models._prefer_secondary(models.Tier.FAST, priority) is expected


def test_background_overflows_only_after_a_long_backoff(settings, monkeypatch):
    import time

    from mavis.llm import models

    monkeypatch.setattr(models, "_secondary_ready", lambda tier: True)
    models._ollama.backoff_until = time.monotonic() + 30
    assert models._prefer_secondary(models.Tier.FAST, "background") is False
    models._ollama.backoff_until = time.monotonic() + 90
    assert models._prefer_secondary(models.Tier.FAST, "background") is True
    models._ollama.reset()
