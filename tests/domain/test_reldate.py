"""T2: stored titles are time-neutral. One language-level resolver rewrites relative date expressions
against the moment the text was written, in the user's zone. Expressions it cannot resolve without a
guess are kept and the text is marked with when it was said. Varied phrasings, anchors across zones,
year boundaries, idempotence, and text without relative words left alone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from mavis.domain.reldate import absolutize, has_relative

IST, NYC, LON, AKL = "Asia/Kolkata", "America/New_York", "Europe/London", "Pacific/Auckland"
ZONES = [IST, NYC, LON, AKL]


def at(tz: str, y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


SAT = (2026, 10, 3, 18, 48)  # Saturday 3 Oct 2026, 18:48 local

# phrase -> expected, all written at SAT in the user's zone (the same local wall time in every zone)
AT_SAT = [
    ("Block around 2 pm tomorrow", "Block around 2 pm Sun 4 Oct"),
    ("Dinner with Priya tonight", "Dinner with Priya Sat 3 Oct evening"),
    ("Submit the report this Friday", "Submit the report Fri 9 Oct"),
    ("Plan the offsite next week", "Plan the offsite week of 5 Oct"),
    ("Call mom today", "Call mom Sat 3 Oct"),
    ("Pay rent by Monday", "Pay rent by Mon 5 Oct"),
    ("Pay rent by Monday 10am", "Pay rent by Mon 5 Oct 10am"),
    ("Thursday 2pm dentist", "Thu 8 Oct 2pm dentist"),
    ("Follow up with Ravi in 3 days", "Follow up with Ravi on Tue 6 Oct"),
    ("Check back in two weeks", "Check back on Sat 17 Oct"),
    ("Renew passport in a week", "Renew passport on Sat 10 Oct"),
    ("Reply by in 2 days", "Reply by Mon 5 Oct"),
    ("Answer the email from yesterday", "Answer the email from Fri 2 Oct"),
    ("Recap of last Friday's call", "Recap of Fri 2 Oct's call"),
    ("Gym tomorrow morning", "Gym Sun 4 Oct morning"),
    ("Report due the day after tomorrow", "Report due Mon 5 Oct"),
    ("Review this morning's notes", "Review Sat 3 Oct morning's notes"),
    ("Trip this weekend", "Trip weekend of Sat 3 Oct"),
    ("Clean the garage over the weekend", "Clean the garage over weekend of Sat 3 Oct"),
    ("Budget review this month", "Budget review Oct 2026"),
    ("File taxes next month", "File taxes Nov 2026"),
    ("Plan holidays next year", "Plan holidays 2027"),
    ("Sent it 2 days ago", "Sent it on Thu 1 Oct"),
    ("Ping Arun in 2 hours", "Ping Arun at 20:48 on Sat 3 Oct"),
    ("Finish slides by end of the week", "Finish slides by end of week of 28 Sep"),
    ("Finish slides by end of next week", "Finish slides by end of week of 5 Oct"),
    ("Invoice by end of month", "Invoice by end of Oct 2026"),
    ("Recap last week's sprint", "Recap week of 21 Sep's sprint"),
    ("Grocery run on Tues", "Grocery run on Tue 6 Oct"),
    ("Book flights the week after next", "Book flights week of 12 Oct"),
    ("Text Meera 3 weeks from now", "Text Meera on Sat 24 Oct"),
    ("Sleep early last night was rough", "Sleep early Fri 2 Oct night was rough"),
    ("TOMORROW: call the bank", "Sun 4 Oct: call the bank"),
    ("Visa appointment in two days' time", "Visa appointment on Mon 5 Oct"),
    ("Call the plumber in 2 days time", "Call the plumber on Mon 5 Oct"),
    ("Dinner the coming Wednesday", "Dinner Wed 7 Oct"),
    ("Fix today's build", "Fix Sat 3 Oct's build"),
]


@pytest.mark.parametrize("tz", ZONES)
@pytest.mark.parametrize(("text", "expected"), AT_SAT)
def test_resolves_against_the_anchor_in_the_users_zone(text, expected, tz) -> None:
    assert absolutize(text, at(tz, *SAT), tz) == expected


UNCHANGED = [
    "Renew the lease",
    "Team sync Sun 4 Oct 14:00",
    "Meeting Friday, 9 October",
    "Meeting Fri Oct 9 at noon",
    "Standup every Monday",
    "Gym on Mondays",
    "Yoga each Friday",
    "I sat with Jawahar",
    "Wed the plans to the budget",
    "Pay Rs 5000 to the landlord",
    "Review weekend of Sat 10 Oct",
    "Offsite week of 12 Oct",
    "Budget Oct 2026",
    "Weekends are for family",
    "Hear back in a day or two",
    "Reply in a few days",
]


@pytest.mark.parametrize("text", UNCHANGED)
def test_text_without_relative_words_is_unchanged(text) -> None:
    anchor = at(IST, *SAT)
    assert absolutize(text, anchor, IST) == text
    assert has_relative(text) is False


AMBIGUOUS = [
    # said on a Saturday: "Saturday" could be today or next week
    ("Call Tom on Saturday", "Call Tom on Saturday (said Sat 3 Oct 18:48)"),
    # "next Tuesday" means different days to different people
    ("Lunch next Tuesday", "Lunch next Tuesday (said Sat 3 Oct 18:48)"),
    ("Movie next Fri with Sam", "Movie next Fri with Sam (said Sat 3 Oct 18:48)"),
]


@pytest.mark.parametrize(("text", "expected"), AMBIGUOUS)
def test_ambiguous_expressions_are_kept_and_marked_with_when_they_were_said(text, expected) -> None:
    assert absolutize(text, at(LON, *SAT), LON) == expected


def test_resolvable_parts_still_resolve_next_to_an_ambiguous_one() -> None:
    out = absolutize("Prep tomorrow for lunch next Tuesday", at(NYC, *SAT), NYC)
    assert out == "Prep Sun 4 Oct for lunch next Tuesday (said Sat 3 Oct 18:48)"


@pytest.mark.parametrize("tz", ZONES)
def test_just_past_midnight_tomorrow_is_ambiguous_but_tonight_is_not(tz) -> None:
    anchor = at(tz, 2026, 10, 4, 1, 30)  # Sunday 01:30
    assert absolutize("Call the bank tomorrow", anchor, tz) == "Call the bank tomorrow (said Sun 4 Oct 01:30)"
    assert absolutize("Gym tonight", anchor, tz) == "Gym Sun 4 Oct evening"


def test_one_instant_resolves_by_each_users_local_day() -> None:
    instant = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)
    assert absolutize("Gym tomorrow", instant, NYC) == "Gym Sun 4 Oct"        # Sat 16:00
    assert absolutize("Gym tomorrow", instant, LON) == "Gym Sun 4 Oct"        # Sat 21:00
    assert absolutize("Gym tomorrow", instant, AKL) == "Gym Mon 5 Oct"        # Sun 09:00
    assert absolutize("Gym tomorrow", instant, IST) == "Gym tomorrow (said Sun 4 Oct 01:30)"  # 01:30


@pytest.mark.parametrize(("text", "expected"), [
    ("Party tomorrow", "Party Fri 1 Jan 2027"),
    ("Plan next week", "Plan week of 4 Jan 2027"),
    ("Review next month", "Review Jan 2027"),
    ("Goals this year", "Goals 2026"),
    ("Gift this Saturday", "Gift Sat 2 Jan 2027"),
    ("Call in 3 days", "Call on Sun 3 Jan 2027"),
])
def test_year_boundary(text, expected) -> None:
    assert absolutize(text, at(AKL, 2026, 12, 31, 10, 0), AKL) == expected  # Thursday


@pytest.mark.parametrize(("weekday_anchor", "text", "expected"), [
    ((2026, 10, 5, 9, 0), "Ship it this week", "Ship it week of 5 Oct"),             # Monday
    ((2026, 10, 11, 9, 0), "Ship it this week", "Ship it week of 5 Oct"),            # Sunday
    ((2026, 10, 7, 9, 0), "Hike this weekend", "Hike weekend of Sat 10 Oct"),        # Wednesday
    ((2026, 10, 4, 9, 0), "Hike this weekend", "Hike weekend of Sat 3 Oct"),         # Sunday
    ((2026, 10, 4, 9, 0), "Hike next weekend", "Hike weekend of Sat 10 Oct"),        # Sunday
    ((2026, 10, 7, 9, 0), "Hike next weekend", "Hike next weekend (said Wed 7 Oct 09:00)"),
    ((2026, 10, 5, 9, 0), "Photos from last weekend", "Photos from weekend of Sat 3 Oct"),
    ((2026, 10, 7, 9, 0), "Call on Wednesday", "Call on Wednesday (said Wed 7 Oct 09:00)"),
    ((2026, 10, 7, 9, 0), "Call this Wednesday", "Call Wed 7 Oct"),
    ((2026, 10, 7, 9, 0), "Notes from last Wednesday", "Notes from Wed 30 Sep"),
])
def test_weeks_weekends_and_same_weekday(weekday_anchor, text, expected) -> None:
    assert absolutize(text, at(NYC, *weekday_anchor), NYC) == expected


@pytest.mark.parametrize("tz", ZONES)
@pytest.mark.parametrize("text", [t for t, _ in AT_SAT + AMBIGUOUS] + UNCHANGED)
def test_idempotent_under_any_later_anchor(text, tz) -> None:
    once = absolutize(text, at(tz, *SAT), tz)
    for later in (at(tz, 2026, 10, 6, 10, 0), at(tz, 2026, 11, 20, 3, 0), at(tz, 2027, 2, 1, 23, 0)):
        assert absolutize(once, later, tz) == once
    assert has_relative(once) is False or "(said " in once


def test_output_has_no_em_or_en_dashes() -> None:
    for text, _ in AT_SAT + AMBIGUOUS:
        out = absolutize(text, at(IST, *SAT), IST)
        assert "—" not in out and "–" not in out
