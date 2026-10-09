"""Per-user preferences with side effects (spec 5): timezone hooks re-anchor routines; one-off wakeups keep
their absolute instant."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from mavis.access.currency import country_for_zone
from mavis.access.tz_resolve import valid_zone
from mavis.store.repo import users

log = structlog.get_logger(__name__)

TimezoneHook = Callable[[int, str, str], Awaitable[None]]
_tz_hooks: list[TimezoneHook] = []


@dataclass(frozen=True)
class TimezoneChange:
    old: str
    new: str


def register_timezone_hook(fn: TimezoneHook) -> None:
    if fn not in _tz_hooks:
        _tz_hooks.append(fn)


async def set_timezone(user_id: int, tz: str) -> TimezoneChange:
    if not valid_zone(tz):
        raise ValueError(f"unknown time zone {tz!r}")
    old = (await users.get(user_id)).timezone
    await users.update(user_id, timezone=tz, country=country_for_zone(tz))
    for fn in list(_tz_hooks):
        try:  # the zone is saved: one failing re-anchor must not skip the others
            await fn(user_id, old, tz)
        except Exception as exc:  # noqa: BLE001
            log.warning("preferences.timezone_hook_failed", hook=getattr(fn, "__name__", "?"),
                        error=type(exc).__name__)
    return TimezoneChange(old, tz)


async def set_currency(user_id: int, code: str) -> str:
    code = code.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency must be a 3-letter code")
    await users.update(user_id, currency=code)
    return code


async def set_name(user_id: int, name: str) -> str:
    name = " ".join(name.split())[:60]
    if not name:
        raise ValueError("empty name")
    await users.update(user_id, name=name)
    return name
