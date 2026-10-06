"""T5: the morning check-in time is learned from mornings only. Each local day counts with its earliest
message inside the morning window; days with no morning message are ignored, and with fewer than
MIN_SAMPLES morning days the default stands. The result stays clamped."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from mavis.initiative import routines as routines_mod
from mavis.initiative.composer import Composer
from mavis.initiative.executor import InitiativeExecutor
from mavis.initiative.quiet import QuietTracker
from mavis.initiative.routines import Routines
from mavis.loops.service import LoopService
from mavis.policy.pings import PingPolicy
from mavis.store.db import Session
from mavis.store.models import Message
from mavis.store.repo import users
from mavis.timers.service import WakeupService

IST, NYC, LON, AKL = "Asia/Kolkata", "America/New_York", "Europe/London", "Pacific/Auckland"
DEFAULT = time(8, 30)


def local(tz: str, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(2026, 10, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


async def _setup(user, recording_bus, fake_memory, clock, tz: str, stamps: list[tuple[int, int, int]]):
    await users.update(user.id, timezone=tz)
    user = await users.get(user.id)
    clock.set(local(tz, 11, 12, 0))  # Sunday 11 Oct; the week before is Sun 4 Oct to Sat 10 Oct
    async with Session() as s:
        for d, h, mi in stamps:
            s.add(Message(user_id=user.id, role="user", content="hi", proactive=False,
                          created_at=local(tz, d, h, mi)))
        await s.commit()
    wakeups, loops = WakeupService(), LoopService(recording_bus)
    executor = InitiativeExecutor(recording_bus, loops, wakeups, PingPolicy(), Composer(fake_memory),
                                  QuietTracker(wakeups))
    return user, Routines(loops, wakeups, executor)


@pytest.fixture(autouse=True)
def _no_sources():
    routines_mod.clear_brief_sources()
    yield
    routines_mod.clear_brief_sources()


@pytest.mark.parametrize("tz", [IST, NYC, LON, AKL])
async def test_evening_only_days_are_ignored(user, clock, recording_bus, fake_memory, tz):
    # the prod drift: first messages of the day in the evening pulled the median to the clamp
    weekdays = [(5, 19, 0), (6, 20, 30), (7, 18, 45), (8, 21, 0), (9, 22, 10)]
    user, routines = await _setup(user, recording_bus, fake_memory, clock, tz, weekdays)
    assert await routines.learned_checkin_time(user, weekend=False) == DEFAULT


@pytest.mark.parametrize("tz", [IST, NYC, LON, AKL])
async def test_mixed_week_uses_only_the_mornings(user, clock, recording_bus, fake_memory, tz):
    stamps = [
        (5, 8, 50), (5, 19, 0),   # Mon: morning counts
        (6, 20, 0),               # Tue: evening only, ignored
        (7, 9, 40), (7, 9, 55),   # Wed: the earliest morning message counts
        (8, 2, 15), (8, 9, 30),   # Thu: a 02:15 message is the night before, not a morning
        (9, 23, 0),               # Fri: ignored
    ]
    user, routines = await _setup(user, recording_bus, fake_memory, clock, tz, stamps)
    # mornings 08:50, 09:40, 09:30 -> median 09:30, minus the 30 minute lead
    assert await routines.learned_checkin_time(user, weekend=False) == time(9, 0)


async def test_one_morning_is_not_enough(user, clock, recording_bus, fake_memory):
    user, routines = await _setup(user, recording_bus, fake_memory, clock, IST,
                                  [(5, 9, 0), (6, 19, 0), (7, 20, 0)])
    assert await routines.learned_checkin_time(user, weekend=False) == DEFAULT


async def test_weekend_and_weekday_are_learned_separately(user, clock, recording_bus, fake_memory):
    stamps = [(5, 7, 40), (6, 7, 50), (7, 8, 0),       # weekdays: early
              (11, 10, 50), (10, 11, 10), (10, 21, 0)]  # Sat 10, Sun 11: late mornings, and an evening
    user, routines = await _setup(user, recording_bus, fake_memory, clock, LON, stamps)
    assert await routines.learned_checkin_time(user, weekend=False) == time(7, 20)
    assert await routines.learned_checkin_time(user, weekend=True) == time(10, 30)


@pytest.mark.parametrize(("stamps", "expected"), [
    ([(5, 5, 10), (6, 5, 20), (7, 5, 30)], time(7, 0)),       # 05:20 - 30 = 04:50 -> clamped to 07:00
    ([(5, 11, 50), (6, 11, 55), (7, 11, 58)], time(11, 0)),   # 11:55 - 30 = 11:25 -> clamped to 11:00
])
async def test_still_clamped(user, clock, recording_bus, fake_memory, stamps, expected):
    user, routines = await _setup(user, recording_bus, fake_memory, clock, NYC, stamps)
    assert await routines.learned_checkin_time(user, weekend=False) == expected


async def test_window_bounds(user, clock, recording_bus, fake_memory):
    # 04:59 and 12:00 are outside the morning; 05:00 and 11:59 are inside
    stamps = [(5, 4, 59), (6, 4, 30), (6, 12, 0), (7, 5, 0), (8, 11, 59), (9, 10, 0)]
    user, routines = await _setup(user, recording_bus, fake_memory, clock, AKL, stamps)
    # mornings 05:00, 11:59 and 10:00 -> median 10:00, minus 30 -> 09:30
    assert await routines.learned_checkin_time(user, weekend=False) == time(9, 30)
