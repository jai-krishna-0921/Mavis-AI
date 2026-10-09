from __future__ import annotations

import fakeredis.aioredis
import pytest

from mavis.machine import quota as q
from mavis.store.repo import machine as repo
from mavis.store.repo import users


async def _uid(chat=91_001, tz="Asia/Kolkata"):
    u, _ = await users.get_or_create_by_chat(chat, "Q")
    await users.update(u.id, timezone=tz)
    return u.id


@pytest.mark.parametrize("kind,wall,expected", [("code", 3600, 2 * 0.0895 + 4 * 0.00945),
                                                ("browser", 1800, (2 * 0.0895 + 4 * 0.00945) / 2),
                                                ("code", 0, 0.0)])
def test_cost_is_an_upper_bound_from_settings(settings, kind, wall, expected):
    assert q.Meter().cost(kind, wall) == pytest.approx(expected)


@pytest.mark.parametrize("tz", ["Asia/Kolkata", "America/New_York", "Pacific/Auckland"])
async def test_daily_minutes_refuse_in_the_users_own_day(db, settings, tz, monkeypatch):
    uid = await _uid(91_000 + len(tz), tz)
    meter = q.Meter()
    today = q.local_day(tz)
    await repo.record_usage(uid, today, "agentcore", "code", task_id=None, session_id=f"s-{tz}",
                            wall_s=61 * 60, est_cost_usd=0.1)
    assert await meter.refusal(uid) == q.DAILY_TEXT


async def test_override_to_zero_refuses_at_once(db, settings):
    uid = await _uid(91_100)
    await repo.set_quota(uid, "daily_minutes", 0)
    assert await q.Meter().refusal(uid) == q.DAILY_TEXT


async def test_monthly_budget(db, settings):
    uid = await _uid(91_200)
    today = q.local_day("Asia/Kolkata")
    for i in range(3):
        await repo.record_usage(uid, today.replace(day=1) if today.day > 1 else today, "agentcore", "code",
                                task_id=None, session_id=f"m{i}", wall_s=10, est_cost_usd=2.0)
    assert await q.Meter().refusal(uid) == q.MONTHLY_TEXT


async def test_under_limits_is_allowed(db, settings):
    assert await q.Meter().refusal(await _uid(91_300)) is None


@pytest.mark.parametrize("backend", ["local", "redis"])
async def test_global_slots_cap_and_release(settings, monkeypatch, backend):
    client = fakeredis.aioredis.FakeRedis() if backend == "redis" else None
    monkeypatch.setattr(q, "get_redis", lambda: client)
    slots = q.GlobalSlots(size=2)
    assert await slots.acquire(1, wait_s=0) and await slots.acquire(2, wait_s=0)
    assert not await slots.acquire(3, wait_s=0.2)
    await slots.release(1)
    assert await slots.acquire(3, wait_s=0.5)
    assert sorted(await slots.held()) == [2, 3]


async def test_reacquire_by_the_same_task_is_free(settings, monkeypatch):
    monkeypatch.setattr(q, "get_redis", lambda: None)
    slots = q.GlobalSlots(size=1)
    assert await slots.acquire(9, wait_s=0) and await slots.acquire(9, wait_s=0)


def test_refusal_texts_have_no_dashes():
    for t in (q.DAILY_TEXT, q.MONTHLY_TEXT, q.BUSY_TEXT, q.CONCURRENT_TEXT):
        assert "\u2014" not in t and "\u2013" not in t


@pytest.mark.parametrize("tz", ["Not/AZone", "", "Mars/Olympus"])
def test_unknown_zone_falls_back_to_utc(tz):
    from mavis.store.db import utcnow

    assert q.local_day(tz) == utcnow().date()


@pytest.mark.parametrize("override,limit_ok", [(0.5, False), (200, True)])
async def test_monthly_override_wins_over_the_default(db, settings, override, limit_ok):
    uid = await _uid(91_400 + int(override))
    await repo.record_usage(uid, q.local_day("Asia/Kolkata"), "agentcore", "code", task_id=None,
                            session_id=f"o{override}", wall_s=10, est_cost_usd=1.0)
    await repo.set_quota(uid, "monthly_usd", override)
    assert (await q.Meter().refusal(uid) is None) is limit_ok
