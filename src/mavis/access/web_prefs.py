"""Per-user preferences set from the dashboard, kept in users.state["web_prefs"] and honoured by the quiet
hours policy, the morning check-in and the register mirroring. Name, time zone and the proactive channel use
their existing storage (access.preferences, channels.routing)."""

from __future__ import annotations

import re
from typing import Any

from mavis.config import get_settings
from mavis.store.models import User
from mavis.store.repo import users

KEY = "web_prefs"
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class PrefError(ValueError):
    """The value is not acceptable. The message is safe to show."""


def stored(user: User) -> dict[str, Any]:
    raw = (getattr(user, "state", None) or {}).get(KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _hhmm(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HHMM.match(value):
        raise PrefError(f"{label} must be a time like 08:30.")
    return value


def quiet_hours(user: User) -> tuple[int, int]:
    """(start hour, end hour) in the user's local time: their choice, else the deployment default."""
    q = stored(user).get("quiet_hours")
    if isinstance(q, dict) and _HHMM.match(str(q.get("start"))) and _HHMM.match(str(q.get("end"))):
        return int(q["start"][:2]), int(q["end"][:2])
    s = get_settings()
    return s.quiet_start, s.quiet_end


def morning_time(user: User) -> str | None:
    value = stored(user).get("morning_checkin_time")
    return value if isinstance(value, str) and _HHMM.match(value) else None


def register_opt_out(user: User) -> bool:
    return bool(stored(user).get("language_register_opt_out"))


async def update(user_id: int, patch: dict[str, Any]) -> None:
    """Validate and store the given fields. Quiet hours apply by the hour, so they are stored as whole
    hours."""
    new: dict[str, Any] = {}
    if "quiet_hours" in patch:
        q = patch["quiet_hours"]
        if q is None:
            new["quiet_hours"] = None
        elif isinstance(q, dict):
            start, end = _hhmm(q.get("start"), "Quiet hours start"), _hhmm(q.get("end"), "Quiet hours end")
            new["quiet_hours"] = {"start": f"{start[:2]}:00", "end": f"{end[:2]}:00"}
        else:
            raise PrefError("Quiet hours need a start and an end time.")
    if "morning_checkin_time" in patch:
        m = patch["morning_checkin_time"]
        new["morning_checkin_time"] = None if m is None else _hhmm(m, "The check-in time")
    if "language_register_opt_out" in patch:
        new["language_register_opt_out"] = bool(patch["language_register_opt_out"])
    if new:
        await users.update_nested(user_id, KEY, new)
