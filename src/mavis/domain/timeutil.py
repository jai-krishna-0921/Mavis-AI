"""Clock and timezone helpers.

Every "now" in the app goes through `now()` so tests (and demos) can move time.
Stored datetimes are UTC-aware; local time is only for prompts and display.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from mavis.config import get_settings


def _system_clock() -> datetime:
    return datetime.now(UTC)


_clock: Callable[[], datetime] = _system_clock


def now() -> datetime:
    return _clock()


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Naive values (SQLite reads) are UTC by convention; aware values are converted."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_local(dt: datetime, tz: str) -> datetime:
    return ensure_utc(dt).astimezone(ZoneInfo(tz))  # type: ignore[union-attr]


def to_utc(dt: datetime, tz: str) -> datetime:
    """Naive datetimes are wall-clock time in `tz`; aware ones keep their offset."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(UTC)


AMBIGUOUS_UNTIL_HOUR = 5  # 00:00-04:59 local: "tomorrow" may mean today


def is_ambiguous_day_window(now_local: datetime) -> bool:
    return now_local.hour < AMBIGUOUS_UNTIL_HOUR


def scale_offset(delta: timedelta) -> timedelta:
    """Compress agent wakeup offsets for stage demos (DEMO_TIME_SCALE < 1)."""
    scale = get_settings().demo_time_scale
    return delta * scale if scale > 0 else delta


DAY_QUESTION_PREFIX = "Since it's just past midnight"
_TOMORROW = re.compile(r"\b(tomorrow|tmrw|tmr|tomorow)\b", re.IGNORECASE)


def _day_label(d: datetime) -> str:
    return f"{d:%A} {d:%b} {d.day}"


def needs_day_clarification(text: str, now_local: datetime) -> str | None:
    """The question to ask before acting, or None if the day is unambiguous."""
    if not is_ambiguous_day_window(now_local) or not _TOMORROW.search(text):
        return None
    today, nxt = _day_label(now_local), _day_label(now_local + timedelta(days=1))
    return f"{DAY_QUESTION_PREFIX}, do you mean today ({today}) or {nxt}?"


def time_guidance(now_local: datetime) -> str:
    """Prompt lines that anchor relative dates for the extraction model."""
    lines = [
        f"Current local time: {now_local:%A %Y-%m-%d %H:%M} ({now_local.tzinfo}).",
        "Resolve relative dates against this local time and output ISO-8601 with the local UTC offset.",
    ]
    if is_ambiguous_day_window(now_local):
        lines.append(
            "It is just past midnight: 'tomorrow' could mean today or the next day. "
            "For such events set ambiguous=true and starts_at=null."
        )
    return "\n".join(lines)
