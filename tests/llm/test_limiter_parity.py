"""The Redis Lua script and mavis.llm.share must decide the same way (the capacity rules win)."""

from __future__ import annotations

import itertools
import time

from mavis.llm import limiter, limiter_lua, share

P = "mavis:llm:par:"
KEYS = [P + "holders", P + "queue", P + "last_used", P + "last_chat_start", P + "last_be_start"]
BIG = 10_000.0


async def _decide(client, *, size, chat, be, higher, activity, chat_start, be_start, waited, rank,
                  grace=20.0):
    await client.flushall()
    now = int(time.time() * 1000)
    for i, (kind, r) in enumerate([("c", 0)] * chat + [("b", 2)] * be):
        await client.zadd(P + "holders", {f"{kind}{i}": now + 60_000})
        await client.hset(P + f"holder:{kind}{i}", mapping={"uid": 1, "rank": r})
    if higher:
        await client.zadd(P + "queue", {"hw": now - 1000})
        await client.hset(P + "waiter:hw", mapping={"uid": 2, "rank": 0, "since": now - 1000})
    for key, age in ((KEYS[2], activity), (KEYS[3], chat_start), (KEYS[4], be_start)):
        if age < BIG:
            await client.set(key, now - int(age * 1000))
    c = limiter.policy_constants()
    c["grace_s"] = grace
    script = client.register_script(limiter_lua.TRY_ACQUIRE)
    return [int(x) for x in await script(keys=KEYS, args=[
        now, "me", 7, rank, now - int(waited * 1000), size, 9, 9, 60_000, P, 5000,
        *[int(c[k] * 1000) for k in limiter._CONSTANT_ORDER]])]


def _expected(size, chat, be, higher, activity, chat_start, be_start, waited):
    """(code, clamp_s) the in-process limiter's rules give a best_effort caller."""
    work_active = higher or activity < 20.0
    if size == 1:
        if work_active:
            return 2, None
        return (1, share.TIMEOUT_S) if size - chat - be > 0 else (0, None)
    v = share.best_effort_verdict(share.SlotState(
        size=size, free=size - chat - be, best_effort_inflight=be, interactive_waiting=higher,
        chat_in_flight=chat, since_chat_activity_s=activity, since_chat_start_s=chat_start,
        since_best_effort_start_s=be_start, waited_s=waited))
    return (1, v.timeout_s) if v.start else (0, None)


async def test_best_effort_decisions_match_the_shared_policy(fake_redis, settings):
    checked = 0
    for size, chat, be, higher, activity, chat_start, be_start, waited in itertools.product(
            (1, 2, 3, 6), (0, 1, 2), (0, 1, 2), (False, True), (BIG, 1.0, 10.0), (BIG, 1.0, 10.0),
            (BIG, 1.0, 5.0), (0.0, 30.0, 70.0)):
        if chat + be > size or (chat_start < activity - 100):
            continue
        want = _expected(size, chat, be, higher, activity, chat_start, be_start, waited)
        code, _qlen, clamp_ms = await _decide(
            fake_redis, size=size, chat=chat, be=be, higher=higher, activity=activity, chat_start=chat_start,
            be_start=be_start, waited=waited, rank=2)
        got = (code, clamp_ms / 1000 if clamp_ms else None)
        state = f"{size=} {chat=} {be=} {higher=} {activity=} {chat_start=} {be_start=} {waited=}"
        assert got == want, f"{state}: {got} != {want}"
        checked += 1
    assert checked > 300


async def test_interactive_takes_any_free_slot_even_the_last(fake_redis, settings):
    for size, chat, be, expect in ((3, 0, 0, 1), (3, 2, 0, 1), (3, 3, 0, 0), (3, 1, 1, 1), (1, 0, 0, 1),
                                   (1, 1, 0, 0)):
        code, *_ = await _decide(fake_redis, size=size, chat=chat, be=be, higher=False, activity=1.0,
                                 chat_start=1.0, be_start=BIG, waited=0.0, rank=0)
        assert code == expect, (size, chat, be)
