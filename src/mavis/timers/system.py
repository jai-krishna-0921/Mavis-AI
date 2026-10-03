"""System wakeups (kind 'system_*') are plumbing, not initiative: they never reach the reasoner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mavis.domain.events import Event

SYSTEM_PREFIX = "system_"
SystemWakeupHandler = Callable[[int, str], Awaitable[None]]  # (user_id, reason)
# (user_id, reason, wakeup payload): for handlers whose wakeup carries data
SystemWakeupPayloadHandler = Callable[[int, str, dict[str, Any]], Awaitable[None]]
SYSTEM_WAKEUP_HANDLERS: dict[str, SystemWakeupHandler] = {}
_WITH_PAYLOAD: set[str] = set()


def register_system_wakeup(kind: str, fn: SystemWakeupHandler | SystemWakeupPayloadHandler, *,
                           with_payload: bool = False) -> None:
    if not kind.startswith(SYSTEM_PREFIX):
        raise ValueError(f"system wakeup kinds must start with {SYSTEM_PREFIX!r}: {kind}")
    SYSTEM_WAKEUP_HANDLERS[kind] = fn  # type: ignore[assignment]
    (_WITH_PAYLOAD.add if with_payload else _WITH_PAYLOAD.discard)(kind)


async def dispatch_system_wakeup(event: Event) -> bool:
    """True if this WAKEUP was a system one (handled or not) and must not go to the initiative agent."""
    kind = str(event.payload.get("kind", ""))
    if not kind.startswith(SYSTEM_PREFIX):
        return False
    fn = SYSTEM_WAKEUP_HANDLERS.get(kind)
    if fn is not None:
        reason = str(event.payload.get("reason", ""))
        if kind in _WITH_PAYLOAD:
            await fn(event.user_id, reason, dict(event.payload))  # type: ignore[call-arg]
        else:
            await fn(event.user_id, reason)
    return True
