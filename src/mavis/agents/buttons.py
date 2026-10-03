"""Inline-button dispatch: handlers register by callback-data prefix; the longest prefix wins.

Prefixes: `conn:` (integrations wiring) and `ap:` (approvals, registered once by `agents.wiring`).
BUTTON_PRESSED has exactly one event handler, `dispatch_button`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from mavis.domain.events import Event

log = structlog.get_logger(__name__)

ButtonFn = Callable[[Event, str], Awaitable[None]]  # (event, callback data)
BUTTON_HANDLERS: dict[str, ButtonFn] = {}


def register_button_handler(prefix: str, fn: ButtonFn) -> None:
    BUTTON_HANDLERS[prefix] = fn


async def dispatch_button(event: Event) -> bool:
    """Run the handler for the longest matching prefix. True when a handler ran."""
    data = str(event.payload.get("data", ""))
    matches = [p for p in BUTTON_HANDLERS if data.startswith(p)]
    if not matches:
        log.debug("buttons.unknown", data=data[:40])
        return False
    await BUTTON_HANDLERS[max(matches, key=len)](event, data)
    return True


APPROVAL_PREFIX = "ap:"


async def _approval_button(event: Event, data: str) -> None:
    from mavis.policy import approvals  # lazy: policy.approvals imports the bus and the LLM layer

    await approvals.handle_approval_button(event)


def register_approval_buttons() -> None:
    """Route `ap:<id>:<ok|edit|no>` taps to the approval flow (the one BUTTON_PRESSED dispatcher)."""
    register_button_handler(APPROVAL_PREFIX, _approval_button)
