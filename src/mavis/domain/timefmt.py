"""How a due time relates to now, computed in code once (phase A3).

Every place that shows a due time to the model or the user renders it through `relative_due` with the
clock read at render time, so no prompt ever asks the model to subtract two timestamps, and an overdue
item is never presented as upcoming.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

NOW_WINDOW = timedelta(seconds=60)    # within a minute either side: "due now"
IMMINENT_WITHIN = timedelta(hours=1)
SOON_WITHIN = timedelta(hours=24)


class DueStatus(StrEnum):
    OVERDUE = "overdue"
    IMMINENT = "imminent"  # now, or within the hour
    SOON = "soon"          # within a day
    LATER = "later"
    NONE = "none"          # no due time


@dataclass(frozen=True)
class RelativeDue:
    status: DueStatus
    label: str


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def duration(minutes: int) -> str:
    """'12 min', '5h 48m', '8h', '1 day', '2 days 5h' (minutes dropped once it is days)."""
    minutes = max(minutes, 1)
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 24 * 60:
        h, m = divmod(minutes, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    days, rest = divmod(minutes, 24 * 60)
    h = rest // 60
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} {h}h" if h else f"{days} {unit}"


def _day(local: datetime, today: datetime) -> str:
    """'Mon 5 Oct', with the year only when it is not this year."""
    year = f" {local.year}" if local.year != today.year else ""
    return f"{local:%a} {local.day} {local:%b}{year}"


def _when(local: datetime, local_now: datetime) -> str:
    """A past or future moment as the user reads it: '13:00', 'yesterday 22:00', 'Thu 1 Oct 13:00'."""
    days = (local.date() - local_now.date()).days
    hhmm = f"{local:%H:%M}"
    if days == 0:
        return hhmm
    if days == -1:
        return f"yesterday {hhmm}"
    if days == 1:
        return f"tomorrow {hhmm}"
    return f"{_day(local, local_now)} {hhmm}"


def relative_due(due: datetime | None, now: datetime, tz: str) -> RelativeDue:
    if due is None:
        return RelativeDue(DueStatus.NONE, "no due date")
    zone = ZoneInfo(tz)
    due_utc, now_utc = _utc(due), _utc(now)
    local, local_now = due_utc.astimezone(zone), now_utc.astimezone(zone)
    delta = due_utc - now_utc
    if abs(delta) < NOW_WINDOW:
        return RelativeDue(DueStatus.IMMINENT, "due now")
    if delta < timedelta(0):
        late = int(-delta.total_seconds() // 60)
        label = f"overdue by {duration(late)} (was due {_when(local, local_now)})"
        return RelativeDue(DueStatus.OVERDUE, label)
    ahead = math.ceil(delta.total_seconds() / 60)
    if delta <= IMMINENT_WITHIN:
        return RelativeDue(DueStatus.IMMINENT, f"due in {duration(ahead)} ({local:%H:%M})")
    status = DueStatus.SOON if delta <= SOON_WITHIN else DueStatus.LATER
    days = (local.date() - local_now.date()).days
    if days == 0:
        return RelativeDue(status, f"due today {local:%H:%M} (in {duration(ahead)})")
    if days == 1:
        return RelativeDue(status, f"due tomorrow {local:%H:%M}")
    return RelativeDue(status, f"due {_day(local, local_now)} {local:%H:%M}")


def relative_past(at: datetime, now: datetime, tz: str) -> str:
    """When something happened, as the user reads it: 'just now', '12 min ago', '5h 48m ago' (same day),
    else 'yesterday 22:48' or 'Wed 30 Sep 09:18'."""
    zone = ZoneInfo(tz)
    at_utc, now_utc = _utc(at), _utc(now)
    ago = now_utc - at_utc
    if ago < NOW_WINDOW:
        return "just now"
    local, local_now = at_utc.astimezone(zone), now_utc.astimezone(zone)
    if local.date() == local_now.date():
        return f"{duration(int(ago.total_seconds() // 60))} ago"
    return _when(local, local_now)


def due_label(due: datetime | None, now: datetime, tz: str) -> str:
    return relative_due(due, now, tz).label


# --- stamps on replayed messages (T1) ----------------------------------------------------------------
# A replayed message carries no clock of its own, so the model reads "today" in a two-day-old message as
# today. Every replay prefixes each stored message with this stamp, computed against the clock at replay
# time in the user's zone. The grammar is closed (STAMP), so an echoed stamp can be stripped from output.

_HHMM = r"\d{2}:\d{2}"
_DAYNAME = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} [A-Z][a-z]{2}(?: \d{4})?"
STAMP = re.compile(
    rf"\[(?:just now|earlier today {_HHMM}|yesterday, {_DAYNAME} {_HHMM}|\d+ days ago, {_DAYNAME} {_HHMM})\]"
)
_LEADING_STAMP = re.compile(rf"^[ \t]*{STAMP.pattern}[ \t]*", re.MULTILINE)
_ANY_STAMP = re.compile(rf"[ \t]*{STAMP.pattern}")


def message_stamp(at: datetime, now: datetime, tz: str) -> str:
    """'[just now]', '[earlier today 09:12]', '[yesterday, Mon 5 Oct 22:00]', '[2 days ago, Sun 4 Oct 15:46]'.

    Days are counted by the user's local calendar, so a message 15 minutes before local midnight is
    'yesterday'. A time in the future (clock skew) reads as 'just now'."""
    zone = ZoneInfo(tz)
    at_utc, now_utc = _utc(at), _utc(now)
    if now_utc - at_utc < NOW_WINDOW:
        return "[just now]"
    local, local_now = at_utc.astimezone(zone), now_utc.astimezone(zone)
    days = (local_now.date() - local.date()).days
    hhmm = f"{local:%H:%M}"
    if days <= 0:
        return f"[earlier today {hhmm}]"
    if days == 1:
        return f"[yesterday, {_day(local, local_now)} {hhmm}]"
    return f"[{days} days ago, {_day(local, local_now)} {hhmm}]"


def absolute_time(at: datetime, tz: str) -> str:
    """'Thu 1 Oct 2026 09:00' in the user's zone: for text that is stored and read later (summaries)."""
    local = _utc(at).astimezone(ZoneInfo(tz))
    return f"{local:%a} {local.day} {local:%b} {local.year} {local:%H:%M}"


def stamped(text: str, at: datetime, now: datetime, tz: str) -> str:
    return f"{message_stamp(at, now, tz)} {text}"


def strip_stamps(text: str) -> str:
    """Remove every replay stamp the model echoed, wherever it appears. The grammar is closed (STAMP), so
    other brackets ([1], [the doc]) stay."""
    return _ANY_STAMP.sub("", _LEADING_STAMP.sub("", text))
