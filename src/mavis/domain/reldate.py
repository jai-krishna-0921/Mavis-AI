"""Make stored text time-neutral: resolve relative date expressions against when the text was written (T2).

A title such as "Block 2 pm tomorrow" is true only on the day it was written; stored and shown days later
it names the wrong day. Every writer of a stored title runs it through `absolutize` with the anchor (the
moment the source text was written) and the user's zone, and the expressions become absolute dates.

This is a language-level normaliser, not a list of known strings. The grammar it understands:

- day words, lowercase only: today, tonight, tomorrow, yesterday (common spellings too), optionally
  followed by morning/afternoon/evening/night; "this morning/afternoon/evening", "last night";
  "the day after tomorrow", "the day before yesterday"
- weekdays, only after a temporal marker (this/coming/next/last/on/by/before/until/till), or at the
  very start of the text followed by a time ("Thursday 2pm dentist"); abbreviations only in Title case
- this/next/last week, the week after next, this/next/last/coming weekend, "<preposition> the weekend",
  this/next/last month and year, end/start/beginning/middle of the day/week/month/year
- offsets: in N minutes/hours/days/weeks, N ... from now, N ... ago (N in digits, a/an or one..twelve);
  minute and hour offsets are added in UTC, so a DST change in between gives the right wall time

It prefers leaving text unchanged over guessing. Nothing is rewritten inside quotes, when glued to
another token ("Monday.com"), as part of a capitalised name ("Black Friday", "Sunday Times", "Today
show", "Wednesday Addams", "The Day After Tomorrow"), before "the <ordinal>" ("Friday the 9th"), or in
recurring phrases ("every Monday", "on Mondays"). Expressions with two common readings stay as written
too: "next <weekday>", a bare weekday said on that same weekday, "next weekend" said on a weekday, "the
coming week", and tomorrow said just past midnight (timeutil's ambiguous window). No marker is added:
the moment the text was written is kept by the caller (created_at, anchor_at).

Resolution conventions (deterministic, in the user's local calendar; weeks start on Monday): tonight is
that day's evening; a this/on/by weekday is its next occurrence; last weekday is the most recent one
before the anchor day; "this weekend" on a Saturday or Sunday is the current one. Output contains no
expression the resolver would rewrite again.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from mavis.domain import timeutil

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
_ABBR = {"Mon": 0, "Tue": 1, "Tues": 1, "Wed": 2, "Thu": 3, "Thur": 3, "Thurs": 3, "Fri": 4, "Sat": 5,
         "Sun": 6}
_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_N = r"\d{1,3}|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_UNIT = r"minutes?|mins?|hours?|hrs?|days?|weeks?"
_PART = r"morning|afternoon|evening|night"
_MARK = r"this|(?:the\s+)?coming|next|last|on|by|before|until|till"
_ORDINAL_AFTER = re.compile(
    r"\s+the\s+(?:\d{1,2}(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|\w+teenth|twentieth|twenty\w*|thirtieth|thirty\w*)\b", re.IGNORECASE)
_TIME_AFTER = re.compile(
    r"\s+(?:at\s+)?(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2})", re.IGNORECASE)
_CAPITAL_AFTER = re.compile(r"\s+(?!I\b)[A-Z][A-Za-z]")
# quoted spans: double, curly and back quotes, and single quotes that open and close at word edges
_QUOTED = re.compile(r"\"[^\"]*\"|\u201c[^\u201d]*\u201d|`[^`]*`|\u2018[^\u2019]*\u2019|(?<!\w)'[^']*'(?!\w)")
_JOINERS = ".-/@_"
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
# a weekday followed by a calendar date is already absolute ("Friday 9 Oct", "Fri, Oct 9")
_DATED = rf"(?!,?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?\b(?!\s*(?:am|pm|a\.m|p\.m|:|h\b))|{_MONTH}\s+\d))"
_FULL_WD = "|".join(_WEEKDAYS)
_ABBR_WD = "|".join(sorted(_ABBR, key=len, reverse=True))

_PATTERN = re.compile(
    r"\b(?:the\s+)?day\s+after\s+(?:tomorrow|tmrw)\b(?P<dat>)"
    r"|\b(?:the\s+)?day\s+before\s+yesterday\b(?P<dby>)"
    r"|\b(?:the\s+)?week\s+after\s+next\b(?P<wan>)"
    rf"|\b(?P<edge>end|start|beginning|middle)\s+of\s+(?:the\s+|this\s+)?(?P<eunit>day|week|month|year)\b(?!\s+of\b)"
    rf"|\b(?P<which>this|next|last|coming)\s+(?P<unit>weekend|week|month|year)\b"
    r"|\b(?P<wkprep>over|on|at|during|for|by|until|till)\s+the\s+weekend\b(?!\s+of\b)"
    rf"|\bthis\s+(?P<tpart>{_PART})\b"
    r"|\blast\s+night\b(?P<last_night>)"
    rf"|\b(?P<day>today|tonight|tonite|tomorrow|tomorow|tommorow|tmrw|tmr|yesterday)\b"
    rf"(?:\s+(?P<part>{_PART})\b)?"
    rf"|\bin\s+(?P<n1>{_N})\s+(?P<u1>{_UNIT})\b(?!\s+or\b)(?:(?:'s|\u2019s|'|\u2019)?\s+time\b)?"
    rf"|\b(?P<n2>{_N})\s+(?P<u2>{_UNIT})\s+(?P<dir>ago|from\s+now)\b"
    rf"|\b(?:(?P<wmark>{_MARK})\s+)?(?P<wd>{_FULL_WD})\b{_DATED}"
    rf"|\b(?P<amark>{_MARK})\s+(?P<wa>(?-i:{_ABBR_WD}))\b{_DATED}",
    re.IGNORECASE,
)
_BARE_PREP = frozenset({"by", "before", "until", "till", "after", "since", "from"})
_KEEP_MARK = frozenset({"on", "by", "before", "until", "till"})
_PREV_WORD = re.compile(r"([A-Za-z]+)\W*$")


def _label(d: date, ref: date) -> str:
    """'Sun 4 Oct', with the year only when it differs from the anchor's."""
    year = f" {d.year}" if d.year != ref.year else ""
    return f"{d:%a} {d.day} {d:%b}{year}"


def _week(monday: date, ref: date) -> str:
    year = f" {monday.year}" if monday.year != ref.year else ""
    return f"week of {monday.day} {monday:%b}{year}"


def _month(year: int, month: int) -> str:
    return f"{date(year, month, 1):%b} {year}"


def _add_months(d: date, n: int) -> tuple[int, int]:
    k = d.year * 12 + d.month - 1 + n
    return k // 12, k % 12 + 1


def _num(raw: str) -> int:
    return int(raw) if raw.isdigit() else _NUMBERS[raw.casefold()]


class _Unresolved(Exception):
    """The expression has two common readings: keep it as written (no guess is frozen into the text)."""


class _Resolver:
    def __init__(self, text: str, local: datetime) -> None:
        self.text = text
        self.local = local
        self.today = local.date()
        self.quoted = [m.span() for m in _QUOTED.finditer(text)]

    def _prev_word(self, start: int) -> str:
        m = _PREV_WORD.search(self.text[:start])
        return m.group(1).casefold() if m else ""

    def temporal(self, m: re.Match) -> bool:
        """The match is unambiguous temporal usage. Prefer leaving text unchanged over guessing: never
        inside quotes, never glued to another token ("Monday.com", "pre-tomorrow"), never part of a
        capitalised name ("Black Friday", "Sunday Times", "Today show", "The Day After Tomorrow")."""
        start, end = m.span()
        text = self.text
        if any(a <= start < b or a < end <= b for a, b in self.quoted):
            return False
        before, after = text[start - 1:start], text[end:end + 2]
        if before and before in _JOINERS and start >= 2 and text[start - 2].isalnum():
            return False
        if len(after) == 2 and after[0] in _JOINERS and after[1].isalnum():
            return False
        if m.group("wd") or m.group("wa"):
            wd_end = m.end("wd") if m.group("wd") else m.end("wa")
            rest = text[wd_end:]
            if _ORDINAL_AFTER.match(rest) or _CAPITAL_AFTER.match(rest):
                return False  # "Friday the 9th", "Wednesday Addams"
            if m.group("wmark") or m.group("amark"):
                return True
            # a bare weekday only at the very start and followed by a time ("Thursday 2pm dentist")
            return not text[:start].strip() and bool(_TIME_AFTER.match(rest))
        # every other expression is temporal only in lowercase ("today", not "Today" or "TODAY")
        return m.group(0) == m.group(0).lower()

    def __call__(self, m: re.Match) -> str:
        if not self.temporal(m):
            return m.group(0)
        try:
            return self._resolve(m)
        except _Unresolved:
            return m.group(0)

    def _tomorrow_ok(self) -> None:
        if timeutil.is_ambiguous_day_window(self.local):
            raise _Unresolved  # just past midnight, "tomorrow" may mean today

    def _resolve(self, m: re.Match) -> str:
        today, g = self.today, m.group
        day = lambda d: _label(d, today)  # noqa: E731
        if g("dat") is not None:
            self._tomorrow_ok()
            return day(today + timedelta(days=2))
        if g("dby") is not None:
            return day(today - timedelta(days=2))
        monday = today - timedelta(days=today.weekday())
        if g("wan") is not None:
            return _week(monday + timedelta(days=14), today)
        if g("edge"):
            unit = g("eunit").casefold()
            span = {"day": day(today), "week": _week(monday, today),
                    "month": _month(today.year, today.month), "year": str(today.year)}[unit]
            return f"{g('edge')} of {span}"
        if g("unit"):
            return self._unit(g("which").casefold(), g("unit").casefold(), monday)
        if g("wkprep"):
            return f"{g('wkprep')} {self._unit('this', 'weekend', monday)}"
        if g("tpart"):
            return f"{day(today)} {g('tpart').casefold()}"
        if g("last_night") is not None:
            return f"{day(today - timedelta(days=1))} night"
        if g("day"):
            word = g("day").casefold()
            part = f" {g('part').casefold()}" if g("part") else ""
            if word in ("tonight", "tonite"):
                return f"{day(today)} evening"
            if word == "today":
                return f"{day(today)}{part}"
            if word == "yesterday":
                return f"{day(today - timedelta(days=1))}{part}"
            self._tomorrow_ok()
            return f"{day(today + timedelta(days=1))}{part}"
        if g("n1") or g("n2"):
            forward = bool(g("n1"))
            n = _num(g("n1") or g("n2"))
            unit = (g("u1") or g("u2")).casefold()
            sign = 1 if forward or g("dir").casefold() != "ago" else -1
            if unit.startswith(("min", "hour", "hr")):
                delta = timedelta(minutes=n) if unit.startswith("min") else timedelta(hours=n)
                # in UTC, then back to local: a DST change in between moves the wall clock correctly
                when = (self.local.astimezone(UTC) + sign * delta).astimezone(self.local.tzinfo)
                core = f"{when:%H:%M} on {day(when.date())}"
                lead = "at "
            else:
                days = n * (7 if unit.startswith("week") else 1)
                core = day(today + timedelta(days=sign * days))
                lead = "on "
            return core if self._prev_word(m.start()) in _BARE_PREP else lead + core
        raw = (g("wmark") or g("amark") or "").split()[-1:]
        raw_mark = raw[0] if raw else ""
        target = _WEEKDAYS[g("wd").casefold()] if g("wd") else _ABBR[g("wa")]
        return self._weekday(raw_mark, target)

    def _unit(self, which: str, unit: str, monday: date) -> str:
        today = self.today
        if unit == "week":
            if which == "coming":
                raise _Unresolved  # "the coming week": the next seven days or next calendar week
            shift = {"this": 0, "next": 7, "last": -7}[which]
            return _week(monday + timedelta(days=shift), today)
        if unit == "weekend":
            wd = today.weekday()
            in_weekend = wd >= 5
            current = today - timedelta(days=wd - 5) if in_weekend else today + timedelta(days=5 - wd)
            if which in ("this", "coming"):
                sat = current
            elif which == "next":
                if not in_weekend:
                    raise _Unresolved  # said on a weekday: this coming weekend or the one after
                sat = current + timedelta(days=7)
            else:  # last
                sat = current - timedelta(days=7)
            return f"weekend of {_label(sat, today)}"
        if unit == "month":
            if which == "coming":
                raise _Unresolved
            return _month(*_add_months(today, {"this": 0, "next": 1, "last": -1}[which]))
        if which == "coming":
            raise _Unresolved
        return str(today.year + {"this": 0, "next": 1, "last": -1}[which])

    def _weekday(self, raw_mark: str, target: int) -> str:
        today, mark = self.today, raw_mark.casefold()
        ahead = (target - today.weekday()) % 7
        if mark == "next":
            raise _Unresolved  # "next Tuesday" means different days to different people
        if mark == "last":
            back = (today.weekday() - target) % 7 or 7
            return _label(today - timedelta(days=back), today)
        if ahead == 0:
            if mark == "this":
                return _label(today, today)
            raise _Unresolved  # "on Saturday" said on a Saturday: today or next week
        resolved = _label(today + timedelta(days=ahead), today)
        return f"{raw_mark} {resolved}" if mark in _KEEP_MARK else resolved


def absolutize(text: str, anchor: datetime, tz: str) -> str:
    """Rewrite unambiguous relative date expressions in `text` as absolute dates, resolved against
    `anchor` (when the text was written) in the user's zone `tz`. Anything else stays as written."""
    if not text:
        return text
    local = timeutil.ensure_utc(anchor).astimezone(ZoneInfo(tz))  # type: ignore[union-attr]
    return _PATTERN.sub(_Resolver(text, local), text)


def has_relative(text: str) -> bool:
    """The text holds a relative date expression the resolver treats as temporal usage."""
    resolver = _Resolver(text or "", datetime(2000, 1, 3, 12, 0, tzinfo=UTC))
    return any(resolver.temporal(m) for m in _PATTERN.finditer(text or ""))
