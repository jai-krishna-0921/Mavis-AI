"""Deterministic resolution of simple relative days in what the user typed.

The extraction model resolves dates itself and sometimes gets them wrong ("by Tuesday" became a
Thursday). For the unambiguous phrases ("by/on <weekday>", "today", "tomorrow") the day is computed
here in the user's timezone and overrides the model's date, keeping the model's time of day.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from mavis.domain import timeutil
from mavis.domain.memory import Extraction

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_DAY_WORD = re.compile(
    rf"\b(?:(?P<prefix>by|on|before|until|till|next|last|this|coming)\s+)?(?P<wd>{_WD})\b"
    r"|\b(?P<rel>today|tonight|tomorrow|tomorow|tmrw|tmr)\b",
    re.IGNORECASE,
)
# explicit calendar dates or other relative phrases: the simple rule would be a guess, so skip
_OTHER_DATES = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)\b|\b\d{1,2}[/-]\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b|\bday after\b|\bweekend\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\bin \d+ days?\b",
    re.IGNORECASE,
)
_ANCHORS = frozenset({"by", "on", "before", "until", "till"})
_WORD = re.compile(r"[a-z0-9]+")
_FILLER = frozenset(
    "a an the at on by in to for of with and or my me i is it this that be will get back up "
    "today tonight tomorrow before until till".split()
)
MAX_SHIFT = timedelta(days=8)


def resolve_relative_day(text: str, now_local: datetime) -> date | None:
    """The single local day the text refers to, or None if there is none or it is ambiguous."""
    if _OTHER_DATES.search(text):
        return None
    days: set[date] = set()
    anchored = False
    for m in _DAY_WORD.finditer(text):
        if rel := m.group("rel"):
            word = rel.casefold()
            if word in ("today", "tonight"):
                days.add(now_local.date())
            else:
                if timeutil.is_ambiguous_day_window(now_local):
                    return None  # just past midnight: "tomorrow" may mean today
                days.add(now_local.date() + timedelta(days=1))
            anchored = True
            continue
        prefix = (m.group("prefix") or "").casefold()
        if prefix in ("next", "last", "this", "coming"):
            return None  # "next Tuesday" means different things to different people
        ahead = (_WEEKDAYS[m.group("wd").casefold()] - now_local.weekday()) % 7
        if ahead == 0:
            return None  # "by Tuesday" said on a Tuesday: today or next week?
        days.add(now_local.date() + timedelta(days=ahead))
        anchored = anchored or prefix in _ANCHORS
    if not anchored or len(days) != 1:
        return None
    return days.pop()


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if w not in _FILLER and w not in _WEEKDAYS}


def _with_day(dt: datetime | None, day: date, tz: str, title: str, said: set[str]) -> datetime | None:
    if dt is None or not (_words(title) & said):
        return dt  # undated, or not obviously about what the user just said
    local = timeutil.to_local(dt, tz)
    if local.date() == day:
        return dt
    moved = local.replace(year=day.year, month=day.month, day=day.day)
    if abs(moved - local) > MAX_SHIFT:
        return dt
    return timeutil.to_utc(moved.replace(tzinfo=None), tz)


def apply_relative_day(extraction: Extraction, user_text: str, now: datetime, tz: str) -> Extraction:
    """Override extracted due dates with the day the user's own words name, when that is clear."""
    day = resolve_relative_day(user_text, timeutil.to_local(now, tz))
    if day is None:
        return extraction
    said = _words(user_text)
    loops = [
        lp.model_copy(update={"due_at": _with_day(lp.due_at, day, tz, f"{lp.title} {' '.join(lp.entities)}",
                                                  said)})
        for lp in extraction.loops
    ]
    events = [
        ev if ev.ambiguous else ev.model_copy(update={
            "starts_at": _with_day(ev.starts_at, day, tz, f"{ev.title} {' '.join(ev.with_people)}", said)})
        for ev in extraction.events
    ]
    return extraction.model_copy(update={"loops": loops, "events": events})
