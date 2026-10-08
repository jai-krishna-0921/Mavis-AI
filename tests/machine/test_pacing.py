"""Global send pacer: at most `rate` sends per second across all chats, Redis or in-memory."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from mavis.channels import pacing


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(pacing, "get_redis", lambda: None)


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(pacing, "get_redis", lambda: client)
    return client


@pytest.mark.parametrize("rate", [1, 3, 25])
async def test_first_rate_reservations_are_free_then_wait(settings, no_redis, monkeypatch, rate):
    monkeypatch.setattr(pacing.time, "time", lambda: 1000.25)
    p = pacing.SendPacer(rate=rate)
    waits = [await p.reserve(chat_id=c) for c in range(rate + 2)]
    assert waits[:rate] == [0.0] * rate
    assert all(0 < w <= 1.0 for w in waits[rate:])


async def test_new_second_resets_the_window(settings, no_redis, monkeypatch):
    now = [50.9]
    monkeypatch.setattr(pacing.time, "time", lambda: now[0])
    p = pacing.SendPacer(rate=1)
    assert await p.reserve() == 0.0
    assert await p.reserve() > 0
    now[0] = 51.01
    assert await p.reserve() == 0.0


async def test_redis_window_is_shared_between_pacers(settings, fake_redis, monkeypatch):
    monkeypatch.setattr(pacing.time, "time", lambda: 7.5)
    a, b = pacing.SendPacer(rate=2), pacing.SendPacer(rate=2)
    assert [await a.reserve(), await b.reserve()] == [0.0, 0.0]
    assert await a.reserve(chat_id=99) > 0


async def test_default_rate_comes_from_settings(settings):
    assert pacing.SendPacer().rate == settings.telegram_global_send_rate
