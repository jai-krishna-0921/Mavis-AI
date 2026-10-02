"""Inline-button dispatch: handlers register by callback-data prefix; the longest prefix wins.

Phase 4 extends this module (adds the `ap:` approval prefix) and must not recreate it.
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


async def dispatch_button(event: Event) -> None:
    data = str(event.payload.get("data", ""))
    matches = [p for p in BUTTON_HANDLERS if data.startswith(p)]
    if not matches:
        log.debug("buttons.unknown", data=data[:40])
        return
    await BUTTON_HANDLERS[max(matches, key=len)](event, data)
