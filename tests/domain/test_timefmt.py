"""A3: one function computes how a due time relates to now, in the user's timezone. Golden cases at fixed
clocks across timezones, plus properties every label must satisfy."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain.timefmt import DueStatus, relative_due

IST, NYC, UTCZ, AKL = "Asia/Kolkata", "America/New_York", "UTC", "Pacific/Auckland"


def at(tz: str, y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


# now, due, tz -> (status, label)
GOLDEN = [
    # the prod case: 13:00 security alert seen at 18:48 IST
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2026, 10, 3, 13, 0), IST,
     DueStatus.OVERDUE, "overdue by 5h 48m (was due 13:00)"),
    # 19:00 workshop seen at 18:48 IST
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2026, 10, 3, 19, 0), IST,
     DueStatus.IMMINENT, "due in 12 min (19:00)"),
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2026, 10, 3, 18, 48), IST, DueStatus.IMMINENT, "due now"),
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2026, 10, 3, 21, 30), IST,
     DueStatus.SOON, "due today 21:30 (in 2h 42m)"),
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2026, 10, 4, 9, 0), IST, DueStatus.SOON, "due tomorrow 09:00"),
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2026, 10, 5, 10, 0), IST, DueStatus.LATER, "due Mon 5 Oct 10:00"),
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2027, 1, 4, 10, 0), IST,
     DueStatus.LATER, "due Mon 4 Jan 2027 10:00"),
    # more than a day overdue
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2026, 10, 1, 13, 0), IST,
     DueStatus.OVERDUE, "overdue by 2 days 5h (was due Thu 1 Oct 13:00)"),
    (at(IST, 2026, 10, 3, 9, 0), at(IST, 2026, 10, 2, 22, 0), IST,
     DueStatus.OVERDUE, "overdue by 11h (was due yesterday 22:00)"),
    (at(IST, 2026, 10, 3, 18, 48), at(IST, 2026, 10, 3, 18, 47), IST,
     DueStatus.OVERDUE, "overdue by 1 min (was due 18:47)"),
    # across midnight: 23:50 now, 00:20 due is "in 30 min", not "today"
    (at(NYC, 2026, 10, 3, 23, 50), at(NYC, 2026, 10, 4, 0, 20), NYC,
     DueStatus.IMMINENT, "due in 30 min (00:20)"),
    (at(NYC, 2026, 10, 3, 23, 50), at(NYC, 2026, 10, 4, 7, 0), NYC, DueStatus.SOON, "due tomorrow 07:00"),
    (at(NYC, 2026, 10, 4, 0, 10), at(NYC, 2026, 10, 3, 23, 40), NYC,
     DueStatus.OVERDUE, "overdue by 30 min (was due yesterday 23:40)"),
    # the same instant reads in each user's own timezone
    (datetime(2026, 10, 3, 12, 0, tzinfo=UTC), datetime(2026, 10, 3, 20, 0, tzinfo=UTC), UTCZ,
     DueStatus.SOON, "due today 20:00 (in 8h)"),
    (datetime(2026, 10, 3, 12, 0, tzinfo=UTC), datetime(2026, 10, 3, 20, 0, tzinfo=UTC), AKL,
     DueStatus.SOON, "due today 09:00 (in 8h)"),  # already 01:00 on the 4th in Auckland
    (datetime(2026, 10, 3, 12, 0, tzinfo=UTC), datetime(2026, 10, 3, 20, 0, tzinfo=UTC), IST,
     DueStatus.SOON, "due tomorrow 01:30"),
    (datetime(2026, 10, 3, 12, 0, tzinfo=UTC), None, IST, DueStatus.NONE, "no due date"),
]


@pytest.mark.parametrize("now,due,tz,status,label", GOLDEN)
def test_golden(now, due, tz, status, label):
    got = relative_due(due, now, tz)
    assert (got.status, got.label) == (status, label)


def test_naive_inputs_are_utc():
    now = datetime(2026, 10, 3, 12, 0)
    assert relative_due(datetime(2026, 10, 3, 12, 30), now, UTCZ).label == "due in 30 min (12:30)"


@pytest.mark.parametrize("tz", [IST, NYC, UTCZ, AKL])
@pytest.mark.parametrize("minutes", [-4000, -1441, -1440, -61, -60, -59, -2, -1, 0, 1, 59, 60, 61, 600,
                                     1439, 1440, 1441, 5000, 60000])
def test_properties(tz, minutes):
    now = datetime(2026, 10, 3, 6, 17, tzinfo=UTC)
    due = now + timedelta(minutes=minutes)
    got = relative_due(due, now, tz)
    if minutes < 0:
        # overdue is never labelled as upcoming
        assert got.status is DueStatus.OVERDUE and got.label.startswith("overdue by ")
        assert not re.search(r"\b(in \d|today|tomorrow|due in)\b", got.label.split("(")[0])
    elif minutes <= 60:
        assert got.status is DueStatus.IMMINENT
    elif minutes <= 1440:
        assert got.status is DueStatus.SOON
    else:
        assert got.status is DueStatus.LATER
    assert "–" not in got.label and "—" not in got.label
