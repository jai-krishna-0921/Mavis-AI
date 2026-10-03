from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mavis.domain import timeutil
from mavis.policy.pings import PingPolicy, in_quiet_hours, next_quiet_end
from mavis.store.db import Session
from mavis.store.models import Message

AFTERNOON = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)  # 13:30 IST
LATE_NIGHT = datetime(2026, 9, 27, 18, 0, tzinfo=UTC)  # 23:30 IST
EARLY_MORNING = datetime(2026, 9, 27, 20, 30, tzinfo=UTC)  # 02:00 IST Mon 28
SEVEN_IST_MON = datetime(2026, 9, 28, 1, 30, tzinfo=UTC)  # 07:00 IST Mon 28


def _ny(user):
    return SimpleNamespace(id=user.id, timezone="America/New_York")


async def _add_proactive(user_id, *times):
    async with Session() as s:
        for i, t in enumerate(times):
            s.add(Message(user_id=user_id, role="assistant", content=f"p{i}", proactive=True, created_at=t))
        await s.commit()


def test_in_quiet_hours_wrapping_and_plain_windows():
    assert in_quiet_hours(23, 23, 7) and in_quiet_hours(2, 23, 7)
    assert not in_quiet_hours(7, 23, 7) and not in_quiet_hours(13, 23, 7)
    assert in_quiet_hours(14, 13, 15) and not in_quiet_hours(15, 13, 15)
    assert not in_quiet_hours(3, 0, 0)  # start == end disables quiet hours


def test_next_quiet_end_boundaries():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    assert next_quiet_end(datetime(2026, 9, 28, 2, 0, tzinfo=tz), 7) == datetime(2026, 9, 28, 7, 0, tzinfo=tz)
    assert next_quiet_end(datetime(2026, 9, 27, 23, 30, tzinfo=tz), 7) == datetime(
        2026, 9, 28, 7, 0, tzinfo=tz
    )
    # exactly at the end hour: strictly after, so the next day
    assert next_quiet_end(datetime(2026, 9, 28, 7, 0, tzinfo=tz), 7) == datetime(2026, 9, 29, 7, 0, tzinfo=tz)


async def test_allowed_in_afternoon(user):
    verdict = await PingPolicy().check(user, 3, None, AFTERNOON)
    assert verdict.allow


async def test_quiet_hours_defer_late_night(user):
    verdict = await PingPolicy().check(user, 3, None, LATE_NIGHT)
    assert not verdict.allow and verdict.defer_until == SEVEN_IST_MON
    assert verdict.reason == "quiet hours"


async def test_quiet_hours_defer_early_morning_same_day(user):
    verdict = await PingPolicy().check(user, 4, None, EARLY_MORNING)
    assert not verdict.allow and verdict.defer_until == SEVEN_IST_MON


async def test_quiet_hours_boundaries_ist(user):
    p = PingPolicy()
    assert not (await p.check(user, 3, None, SEVEN_IST_MON - timedelta(minutes=1))).allow
    assert (await p.check(user, 3, None, SEVEN_IST_MON)).allow
    # 22:59 IST allowed, 23:00 IST deferred
    assert (await p.check(user, 3, None, datetime(2026, 9, 27, 17, 29, tzinfo=UTC))).allow
    assert not (await p.check(user, 3, None, datetime(2026, 9, 27, 17, 30, tzinfo=UTC))).allow


async def test_urgent_bypasses_quiet_hours(user):
    assert (await PingPolicy().check(user, 5, None, EARLY_MORNING)).allow


async def test_defer_new_york_summer(user):
    # 02:00 EDT Jul 1 = 06:00 UTC -> 07:00 EDT = 11:00 UTC same day
    verdict = await PingPolicy().check(_ny(user), 3, None, datetime(2026, 7, 1, 6, 0, tzinfo=UTC))
    assert not verdict.allow and verdict.defer_until == datetime(2026, 7, 1, 11, 0, tzinfo=UTC)


async def test_defer_new_york_across_spring_forward(user):
    # 23:30 EST Mar 7 = 04:30 UTC Mar 8; 07:00 on Mar 8 is EDT (UTC-4) = 11:00 UTC (not 12:00)
    verdict = await PingPolicy().check(_ny(user), 3, None, datetime(2026, 3, 8, 4, 30, tzinfo=UTC))
    assert verdict.defer_until == datetime(2026, 3, 8, 11, 0, tzinfo=UTC)


async def test_defer_new_york_across_fall_back(user):
    # 23:30 EDT Oct 31 = 03:30 UTC Nov 1; 07:00 on Nov 1 is EST (UTC-5) = 12:00 UTC (not 11:00)
    verdict = await PingPolicy().check(_ny(user), 3, None, datetime(2026, 11, 1, 3, 30, tzinfo=UTC))
    assert verdict.defer_until == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


async def test_daily_budget(user, settings, monkeypatch):
    monkeypatch.setattr(settings, "ping_daily_budget", 6)
    await _add_proactive(user.id, *[AFTERNOON - timedelta(minutes=i) for i in range(6)])
    async with Session() as s:
        s.add(
            Message(user_id=user.id, role="assistant", content="reply", proactive=False, created_at=AFTERNOON)
        )
        s.add(Message(user_id=user.id, role="user", content="hi", proactive=True, created_at=AFTERNOON))
        await s.commit()
    policy = PingPolicy()
    assert await policy.count_today(user, AFTERNOON) == 6
    verdict = await policy.check(user, 3, None, AFTERNOON)
    assert not verdict.allow and verdict.reason == "daily budget reached"
    assert verdict.defer_until is None  # over budget: suppressed, not replayed tomorrow
    # F4: urgency 5 bypasses quiet hours only, never the daily budget; it waits for tomorrow
    urgent = await policy.check(user, 5, None, AFTERNOON)
    assert not urgent.allow and urgent.defer_until == SEVEN_IST_MON


async def test_budget_under_limit_allows(user, settings, monkeypatch):
    monkeypatch.setattr(settings, "ping_daily_budget", 6)
    await _add_proactive(user.id, *[AFTERNOON - timedelta(minutes=i) for i in range(5)])
    assert (await PingPolicy().check(user, 3, None, AFTERNOON)).allow


async def test_budget_resets_at_local_midnight(user):
    # IST day Sep 27 is [Sep 26 18:30Z, Sep 27 18:30Z)
    before = datetime(2026, 9, 27, 18, 29, tzinfo=UTC)  # 23:59 IST Sep 27
    after = datetime(2026, 9, 27, 18, 30, tzinfo=UTC)  # 00:00 IST Sep 28
    await _add_proactive(
        user.id, before, before - timedelta(hours=1), datetime(2026, 9, 26, 18, 29, tzinfo=UTC)
    )
    p = PingPolicy()
    assert await p.count_today(user, before) == 2  # the Sep 26 23:59 IST one is yesterday
    assert await p.count_today(user, after) == 0  # new local day
    assert await p.count_today(user, datetime(2026, 9, 27, 2, 0, tzinfo=UTC)) == 2


async def test_budget_counts_per_local_day_new_york(user):
    ny = _ny(user)
    # 21:00 EDT Sep 27 = 01:00 UTC Sep 28 -> still Sep 27 locally
    await _add_proactive(user.id, datetime(2026, 9, 28, 1, 0, tzinfo=UTC))
    p = PingPolicy()
    assert await p.count_today(ny, datetime(2026, 9, 28, 3, 0, tzinfo=UTC)) == 1  # 23:00 EDT Sep 27
    assert await p.count_today(ny, datetime(2026, 9, 28, 4, 0, tzinfo=UTC)) == 0  # 00:00 EDT Sep 28


async def test_dedupe_same_local_day_only(user):
    policy = PingPolicy()
    await policy.record(user, "followup:7", 3, AFTERNOON)
    assert (await policy.check(user, 3, "followup:7", AFTERNOON + timedelta(hours=2))).reason == "duplicate"
    assert (await policy.check(user, 5, "followup:7", AFTERNOON)).allow is False
    assert (await policy.check(user, 3, "followup:7", AFTERNOON + timedelta(days=1))).allow
    assert (await policy.check(user, 3, "followup:8", AFTERNOON)).allow


async def test_dedupe_rolls_over_at_local_midnight(user):
    policy = PingPolicy()
    late = datetime(2026, 9, 27, 18, 29, tzinfo=UTC)  # 23:59 IST
    await policy.record(user, "k", 5, late)
    assert not (await policy.check(user, 5, "k", late)).allow
    assert (await policy.check(user, 5, "k", late + timedelta(minutes=1))).allow  # 00:00 IST next day


async def test_record_twice_is_idempotent(user):
    policy = PingPolicy()
    await policy.record(user, "k", 3, AFTERNOON)
    await policy.record(user, "k", 3, AFTERNOON)
    assert not (await policy.check(user, 3, "k", AFTERNOON)).allow


async def test_record_without_key_is_noop(user):
    await PingPolicy().record(user, None, 3, timeutil.now())


async def test_naive_message_timestamps_from_sqlite_still_count(user):
    await _add_proactive(user.id, AFTERNOON)
    assert await PingPolicy().count_today(user, AFTERNOON) == 1


@pytest.mark.parametrize("urgency", [1, 3, 4])
async def test_non_urgent_levels_are_deferred_in_quiet_hours(user, urgency):
    assert not (await PingPolicy().check(user, urgency, None, LATE_NIGHT)).allow


async def test_recent_user_message_lifts_quiet_hours(user, clock):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    clock.set(LATE_NIGHT - timedelta(minutes=10))
    await messages.log(user.id, Role.USER, "hi")
    clock.set(LATE_NIGHT)
    assert (await PingPolicy().check(user, 3, None, LATE_NIGHT)).allow


async def test_old_user_message_does_not_lift_quiet_hours(user, clock):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    clock.set(LATE_NIGHT - timedelta(minutes=61))
    await messages.log(user.id, Role.USER, "hi")
    clock.set(LATE_NIGHT)
    verdict = await PingPolicy().check(user, 3, None, LATE_NIGHT)
    assert not verdict.allow and verdict.reason == "quiet hours"


async def test_awake_window_is_a_setting(user, clock, monkeypatch):
    from mavis.config import get_settings
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    monkeypatch.setenv("QUIET_AWAKE_WINDOW_MIN", "5")
    get_settings.cache_clear()
    clock.set(LATE_NIGHT - timedelta(minutes=10))
    await messages.log(user.id, Role.USER, "hi")
    clock.set(LATE_NIGHT)
    assert not (await PingPolicy().check(user, 3, None, LATE_NIGHT)).allow


async def test_awake_still_respects_budget_and_dedupe(user, clock):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    clock.set(LATE_NIGHT - timedelta(minutes=1))
    await messages.log(user.id, Role.USER, "hi")
    clock.set(LATE_NIGHT)
    policy = PingPolicy()
    await policy.record(user, "k", 3, LATE_NIGHT)
    assert (await policy.check(user, 3, "k", LATE_NIGHT)).reason == "duplicate"
    await _add_proactive(user.id, *[LATE_NIGHT - timedelta(minutes=i + 2) for i in range(6)])
    assert (await policy.check(user, 3, None, LATE_NIGHT)).reason == "daily budget reached"


async def test_day_key_is_not_dated_twice(user, clock):
    from sqlalchemy import select

    from mavis.store.models import PingLogRow

    policy = PingPolicy()
    await policy.record(user, "morning:2026-09-27", 3, AFTERNOON)
    await policy.record(user, "followup20260927", 3, AFTERNOON)
    await policy.record(user, "prep:7", 3, AFTERNOON)
    async with Session() as s:
        keys = sorted(await s.scalars(select(PingLogRow.key)))
    assert keys == ["followup20260927", "morning:2026-09-27", "prep:7:2026-09-27"]
    assert (await policy.check(user, 3, "morning:2026-09-27", AFTERNOON)).reason == "duplicate"
