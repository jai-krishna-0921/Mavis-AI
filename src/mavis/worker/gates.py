"""Event gates: run before any handler, in order; a gate returning False drops the event (Phase 11).

Gates are for decisions that must happen before routing and never through the LLM: access status, owner
and user commands, cooldowns. They must be idempotent (a retried event runs them again)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from mavis.domain.events import Event

log = structlog.get_logger(__name__)
GateFn = Callable[[Event], Awaitable[bool]]
_gates: dict[str, tuple[int, GateFn]] = {}


def register_event_gate(name: str, fn: GateFn, *, order: int = 50) -> None:
    _gates[name] = (order, fn)


def clear_gates() -> None:
    _gates.clear()


async def run_gates(event: Event) -> bool:
    for name, (_, fn) in sorted(_gates.items(), key=lambda kv: kv[1][0]):
        if not await fn(event):
            log.debug("worker.gate_dropped", gate=name, event_type=event.type)
            return False
    return True
