"""System wakeups (kind 'system_*') are plumbing, not initiative: they never reach the reasoner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from mavis.domain.events import Event

SYSTEM_PREFIX = "system_"
SystemWakeupHandler = Callable[[int, str], Awaitable[None]]  # (user_id, reason)
SYSTEM_WAKEUP_HANDLERS: dict[str, SystemWakeupHandler] = {}


def register_system_wakeup(kind: str, fn: SystemWakeupHandler) -> None:
    if not kind.startswith(SYSTEM_PREFIX):
        raise ValueError(f"system wakeup kinds must start with {SYSTEM_PREFIX!r}: {kind}")
    SYSTEM_WAKEUP_HANDLERS[kind] = fn


async def dispatch_system_wakeup(event: Event) -> bool:
    """True if this WAKEUP was a system one (handled or not) and must not go to the initiative agent."""
    kind = str(event.payload.get("kind", ""))
    if not kind.startswith(SYSTEM_PREFIX):
        return False
    fn = SYSTEM_WAKEUP_HANDLERS.get(kind)
    if fn is not None:
        await fn(event.user_id, str(event.payload.get("reason", "")))
    return True
