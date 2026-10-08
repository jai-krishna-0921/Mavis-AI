"""The Redis Lua script and mavis.llm.policy must decide the same way (e2efix-capacity rules)."""

from __future__ import annotations

import itertools
import time

import pytest

from mavis.llm import limiter_lua, policy

T = policy.Timings()
P = "mavis:llm:par:"


async def _decide(client, *, size, others, be, higher, since_last_s, rank) -> int:
    await client.flushall()
    now = int(time.time() * 1000)
    holders = [("o", 0)] * others + [("b", 2)] * be
    for i, (kind, r) in enumerate(holders):
        lease = f"{kind}{i}"
        await client.zadd(P + "holders", {lease: now + 60_000})
        await client.hset(P + "holder:" + lease, mapping={"uid": 1, "rank": r})
    if higher:
        await client.zadd(P + "queue", {"hw": now - 1000})
        await client.hset(P + "waiter:hw", mapping={"uid": 2, "rank": 0, "since": now - 1000})
    if since_last_s is not None:
        await client.set(P + "last_used", now - int(since_last_s * 1000))
    script = client.register_script(limiter_lua.TRY_ACQUIRE)
    code, _ = await script(keys=[P + "holders", P + "queue", P + "last_used"], args=[
        now, "me", 7, rank, now, size, 9, 9, 60_000, P, 5000, T.share, int(T.grace_s * 1000),
        int(T.quiet_s * 1000), int(T.background_aging_s * 1000), int(T.best_effort_aging_s * 1000)])
    return int(code)


async def test_best_effort_decisions_match_the_policy(fake_redis, settings):
    checked = 0
    for size, others, be, higher, since in itertools.product(
            (1, 2, 3, 6), (0, 1, 2), (0, 1, 2, 3), (False, True), (None, 1.0, 10.0, 100.0)):
        if others + be > size:
            continue
        snap = policy.Snapshot(size, size - others - be, be, higher,
                               1e18 if since is None else since)
        want = 1 if policy.best_effort_may_start(snap, T) else 0
        if policy.single_slot_blocked(snap, T):
            want = 2
        got = await _decide(fake_redis, size=size, others=others, be=be, higher=higher,
                            since_last_s=since, rank=2)
        assert got == want, f"{size=} {others=} {be=} {higher=} {since=}: {got} != {want}"
        checked += 1
    assert checked > 100


@pytest.mark.parametrize("size,others,be,expect", [(3, 0, 0, 1), (3, 2, 0, 1), (3, 3, 0, 0), (3, 1, 1, 1),
                                                   (1, 0, 0, 1), (1, 1, 0, 0)])
async def test_interactive_takes_any_free_slot_even_the_last(fake_redis, settings, size, others, be, expect):
    got = await _decide(fake_redis, size=size, others=others, be=be, higher=False, since_last_s=1.0, rank=0)
    assert got == expect
