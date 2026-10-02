from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from mavis.domain import timeutil

IST = ZoneInfo("Asia/Kolkata")


def test_ambiguous_tomorrow_after_midnight():
    now_local = datetime(2026, 9, 28, 0, 9, tzinfo=IST)
    assert timeutil.is_ambiguous_day_window(now_local)
    question = timeutil.needs_day_clarification("Plan a meeting for tomorrow at 10 am", now_local)
    assert question is not None
    assert "Monday Sep 28" in question
    assert "Tuesday Sep 29" in question


def test_tomorrow_in_afternoon_is_not_ambiguous():
    now_local = datetime(2026, 9, 27, 14, 0, tzinfo=IST)
    assert not timeutil.is_ambiguous_day_window(now_local)
    assert timeutil.needs_day_clarification("tomorrow at 10", now_local) is None


def test_message_without_relative_day_needs_no_clarification():
    now_local = datetime(2026, 9, 28, 0, 9, tzinfo=IST)
    assert timeutil.needs_day_clarification("Monday at 10am with Jawahar", now_local) is None


def test_ambiguity_window_ends_at_five():
    assert timeutil.is_ambiguous_day_window(datetime(2026, 9, 28, 4, 59, tzinfo=IST))
    assert not timeutil.is_ambiguous_day_window(datetime(2026, 9, 28, 5, 0, tzinfo=IST))


def test_monday_10am_ist_to_utc():
    utc = timeutil.to_utc(datetime(2026, 9, 28, 10, 0), "Asia/Kolkata")
    assert utc == datetime(2026, 9, 28, 4, 30, tzinfo=UTC)
    assert timeutil.to_local(utc, "Asia/Kolkata").hour == 10


def test_to_utc_keeps_an_explicit_offset():
    aware = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
    assert timeutil.to_utc(aware, "America/New_York") == datetime(2026, 9, 28, 4, 30, tzinfo=UTC)


def test_ensure_utc_treats_naive_as_utc():
    assert timeutil.ensure_utc(datetime(2026, 1, 1, 12, 0)) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert timeutil.ensure_utc(None) is None


def test_scale_offset_uses_demo_time_scale(settings, monkeypatch):
    monkeypatch.setattr(settings, "demo_time_scale", 0.01)
    assert timeutil.scale_offset(timedelta(hours=1)) == timedelta(seconds=36)


def test_scale_offset_ignores_non_positive_scale(settings, monkeypatch):
    monkeypatch.setattr(settings, "demo_time_scale", 0)
    assert timeutil.scale_offset(timedelta(hours=1)) == timedelta(hours=1)


def test_now_is_patchable(clock):
    clock.set(datetime(2030, 1, 1, tzinfo=UTC))
    assert timeutil.now() == datetime(2030, 1, 1, tzinfo=UTC)


def test_time_guidance_mentions_ambiguity_only_after_midnight():
    assert "ambiguous=true" in timeutil.time_guidance(datetime(2026, 9, 28, 0, 9, tzinfo=IST))
    assert "ambiguous=true" not in timeutil.time_guidance(datetime(2026, 9, 28, 13, 0, tzinfo=IST))
