"""How a due time relates to now, computed in code once (phase A3).

Every place that shows a due time to the model or the user renders it through `relative_due` with the
clock read at render time, so no prompt ever asks the model to subtract two timestamps, and an overdue
item is never presented as upcoming.
"""

from __future__ import annotations

import math
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


def due_label(due: datetime | None, now: datetime, tz: str) -> str:
    return relative_due(due, now, tz).label
