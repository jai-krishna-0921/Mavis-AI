"""T1: every replayed message carries a code-computed stamp relative to now, in the user's zone.

Golden cases at fixed clocks across zones and local day boundaries, plus properties every stamp holds.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mavis.domain.timefmt import STAMP, message_stamp, stamped, strip_stamps

IST, NYC, LON, AKL = "Asia/Kolkata", "America/New_York", "Europe/London", "Pacific/Auckland"


def at(tz: str, y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


GOLDEN = [
    # (message time, now, tz, stamp)
    (at(IST, 2026, 10, 4, 15, 46), at(IST, 2026, 10, 6, 10, 0), IST, "[2 days ago, Sun 4 Oct 15:46]"),
    (at(IST, 2026, 10, 6, 9, 59, ), at(IST, 2026, 10, 6, 10, 0), IST, "[earlier today 09:59]"),
    (at(IST, 2026, 10, 6, 9, 12), at(IST, 2026, 10, 6, 10, 0), IST, "[earlier today 09:12]"),
    (at(IST, 2026, 10, 5, 22, 0), at(IST, 2026, 10, 6, 10, 0), IST, "[yesterday, Mon 5 Oct 22:00]"),
    (at(NYC, 2026, 9, 28, 8, 5), at(NYC, 2026, 10, 6, 7, 0), NYC, "[8 days ago, Mon 28 Sep 08:05]"),
    (at(LON, 2025, 12, 30, 18, 0), at(LON, 2026, 1, 2, 9, 0), LON, "[3 days ago, Tue 30 Dec 2025 18:00]"),
    # 15 minutes ago but across local midnight: it is yesterday for the user
    (at(AKL, 2026, 10, 5, 23, 50), at(AKL, 2026, 10, 6, 0, 5), AKL, "[yesterday, Mon 5 Oct 23:50]"),
]


@pytest.mark.parametrize(("when", "now", "tz", "expected"), GOLDEN)
def test_golden_stamps(when, now, tz, expected) -> None:
    assert message_stamp(when, now, tz) == expected


def test_within_a_minute_is_just_now_and_future_skew_too() -> None:
    now = at(IST, 2026, 10, 6, 10, 0)
    assert message_stamp(now - timedelta(seconds=20), now, IST) == "[just now]"
    assert message_stamp(now + timedelta(seconds=5), now, IST) == "[just now]"


def test_same_instant_reads_by_each_users_local_day() -> None:
    # 10:50 UTC on 5 Oct is 23:50 in Auckland... and 06:50 in New York
    msg = datetime(2026, 10, 5, 10, 50, tzinfo=UTC)
    now = datetime(2026, 10, 5, 11, 30, tzinfo=UTC)  # Auckland: 6 Oct 00:30
    assert message_stamp(msg, now, AKL) == "[yesterday, Mon 5 Oct 23:50]"
    assert message_stamp(msg, now, NYC) == "[earlier today 06:50]"
    assert message_stamp(msg, now, LON) == "[earlier today 11:50]"


def test_naive_times_are_utc() -> None:
    naive = datetime(2026, 10, 4, 10, 16)  # SQLite read: UTC by convention (15:46 IST)
    assert message_stamp(naive, at(IST, 2026, 10, 6, 10, 0), IST) == "[2 days ago, Sun 4 Oct 15:46]"


@pytest.mark.parametrize("tz", [IST, NYC, LON, AKL])
def test_every_stamp_matches_the_strip_grammar(tz) -> None:
    now = at(tz, 2026, 10, 6, 10, 0)
    for minutes in (0, 3, 59, 61, 600, 1440, 2000, 3 * 1440, 40 * 1440, 400 * 1440):
        s = message_stamp(now - timedelta(minutes=minutes), now, tz)
        assert STAMP.fullmatch(s), s
        assert "—" not in s and "–" not in s
        assert strip_stamps(f"{s} hello") == "hello"


def test_stamped_prefixes_and_strip_only_removes_leading_stamps() -> None:
    now = at(IST, 2026, 10, 6, 10, 0)
    text = stamped("lunch tomorrow?", at(IST, 2026, 10, 3, 12, 0), now, IST)
    assert text == "[3 days ago, Sat 3 Oct 12:00] lunch tomorrow?"
    # an echoed stamp is removed wherever it appears; other brackets stay
    assert strip_stamps("[just now] Sure thing.\n[earlier today 09:12] Done") == "Sure thing.\nDone"
    assert strip_stamps("You said [2 days ago, Sun 4 Oct 15:46] that it was tomorrow.") == (
        "You said that it was tomorrow.")
    assert strip_stamps("Done ([yesterday, Mon 5 Oct 22:00]).") == "Done ()."
    assert strip_stamps("Noted [earlier today 09:12]") == "Noted"
    assert strip_stamps("See [1] and [the doc]") == "See [1] and [the doc]"
    assert re.match(r"\[", strip_stamps("[1] first source")) is not None
