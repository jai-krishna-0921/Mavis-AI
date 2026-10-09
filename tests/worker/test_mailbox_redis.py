from __future__ import annotations

import json

from mavis.worker.mailbox import RedisMailbox


async def test_redis_mailbox_fifo_and_single_owner(fake_redis, settings):
    box = RedisMailbox(fake_redis, lease_ms=5000)
    for n in range(4):
        for uid in (5001, 7302, 9944):
            await box.push(uid, "chat", json.dumps({"id": f"{uid}:{n}"}))
    got: dict[int, list[str]] = {}
    owners = ["w-a", "w-b"]
    for step in range(12):
        c = await box.claim("chat", owners[step % 2])
        assert c is not None
        uid, entries = c
        got.setdefault(uid, []).append(json.loads(entries[0])["id"])
        await box.commit(uid, "chat", 1)
        await box.release(uid, "chat", owners[step % 2])
    for uid, ids in got.items():
        assert ids == [f"{uid}:{n}" for n in range(4)]


async def test_reap_requeues_only_unleased_users(fake_redis, settings):
    box = RedisMailbox(fake_redis, lease_ms=60)
    await box.push(1, "bg", "{}")
    await box.push(2, "bg", "{}")
    await box.claim("bg", "dead")
    await box.claim("bg", "alive")
    await fake_redis.zrem("mavis:ready:bg", "u1", "u2")
    import asyncio

    await asyncio.sleep(0.1)
    await box.renew(2, "bg", "alive")
    assert await box.reap() >= 1


async def test_a_leased_user_is_never_claimed_by_a_second_owner(fake_redis, settings):
    box = RedisMailbox(fake_redis, lease_ms=5000)
    await box.push(1, "chat", "{}")
    first = await box.claim("chat", "w-a")
    assert first is not None and first[0] == 1
    await box.push(1, "chat", "{}")  # a new message arrives while w-a is working on this user
    assert await box.claim("chat", "w-b") is None
    await box.commit(1, "chat", 1)
    await box.release(1, "chat", "w-a")  # the second message is picked up after the lease is freed
    second = await box.claim("chat", "w-b")
    assert second is not None and second[0] == 1


async def test_only_the_owner_can_release_a_lease(fake_redis, settings):
    box = RedisMailbox(fake_redis, lease_ms=5000)
    await box.push(2, "bg", "{}")
    await box.claim("bg", "w-a")
    await box.release(2, "bg", "intruder")
    assert await fake_redis.exists("mavis:lease:u2:bg")
    assert not await box.renew(2, "bg", "intruder") and await box.renew(2, "bg", "w-a")


async def test_depth_counts_ready_users_per_lane(fake_redis, settings):
    box = RedisMailbox(fake_redis)
    for uid in (1, 2, 3):
        await box.push(uid, "chat", "{}")
    await box.push(1, "bg", "{}")
    assert (await box.depth("chat"), await box.depth("bg")) == (3, 1)
