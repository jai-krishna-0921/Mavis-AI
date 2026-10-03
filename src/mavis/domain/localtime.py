"""Tool datetimes are the user's wall-clock time; code, never the model, attaches the zone (phase A7).

A model asked for an offset converts times itself and gets it wrong ("11 am" sent as
05:00+05:30). So every datetime a tool takes is the time as the user said it, without offset, plus an
optional IANA `timezone` only when the user names another zone. `localize_args` drops whatever offset
the model wrote, keeps the wall clock, and attaches ZoneInfo(timezone or the user's). It is idempotent,
so a stored, approved call can be re-localized safely.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

WALL_CLOCK_MARK = "the time as the user said it, no offset or Z"
TIMEZONE_FIELD = "timezone"
LOCAL_ONLY_FIELDS = frozenset({TIMEZONE_FIELD})  # read by code, never sent to a provider


def wall_clock(what: str) -> str:
    """The description every tool datetime field carries."""
    return f"{what}. Local wall-clock time, ISO 8601, e.g. 2026-10-04T11:00: {WALL_CLOCK_MARK}."


class LocalTimes(BaseModel):
    """Mixin for every tool args model with a datetime field."""

    timezone: str | None = Field(
        default=None,
        description="IANA timezone, ONLY when the user names another zone (\"3pm New York time\" -> "
                    "America/New_York). Leave empty otherwise; never convert times yourself.",
    )


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def localize_args[M: BaseModel](args: M, user_timezone: str) -> M:
    """Every datetime field: keep the wall clock, drop any model-written offset, attach the named zone
    (args.timezone) or the user's. Date-only fields and non-datetime fields are untouched."""
    zone = _zone(getattr(args, TIMEZONE_FIELD, None)) or _zone(user_timezone) or ZoneInfo("UTC")
    updates = {}
    for name in type(args).model_fields:
        value = getattr(args, name)
        if isinstance(value, datetime):
            local = value.replace(tzinfo=None).replace(tzinfo=zone)
            if local != value or local.utcoffset() != value.utcoffset():
                updates[name] = local
    return args.model_copy(update=updates) if updates else args


def has_datetimes(model: type[BaseModel]) -> bool:
    import typing

    return any(datetime in (typing.get_args(f.annotation) or (f.annotation,))
               for f in model.model_fields.values())


def provider_args(args: BaseModel) -> dict:
    """The arguments a provider receives: local-only hints removed."""
    return args.model_dump(mode="json", exclude=set(LOCAL_ONLY_FIELDS) & set(type(args).model_fields))
