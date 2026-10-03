"""Dispatch LangGraph interrupt payloads by their 'type' ("approval", "connect", ...)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

InterruptHandler = Callable[[int, int, dict[str, Any]], Awaitable[None]]  # (task_id, user_id, payload)
INTERRUPT_HANDLERS: dict[str, InterruptHandler] = {}


def register_interrupt_handler(kind: str, fn: InterruptHandler) -> None:
    INTERRUPT_HANDLERS[kind] = fn


async def dispatch_interrupt(task_id: int, user_id: int, payload: dict[str, Any]) -> bool:
    fn = INTERRUPT_HANDLERS.get(str(payload.get("type", "")))
    if fn is None:
        return False
    await fn(task_id, user_id, payload)
    return True


async def _approval(task_id: int, user_id: int, payload: dict[str, Any]) -> None:
    from mavis.policy import approvals as approval_flow

    await approval_flow.send_approval_prompt(user_id, payload)


async def _connect_unavailable(task_id: int, user_id: int, payload: dict[str, Any]) -> None:
    """Default until Phase 5: say so, then resume the task as 'not connected'."""
    from mavis import bus
    from mavis.domain.events import Job, JobKind
    from mavis.policy import approvals as approval_flow

    cap = str(payload.get("capability", "that account"))
    await approval_flow.say(
        user_id,
        f"I'd need access to your {cap} for part of this, and connecting accounts "
        "isn't switched on yet. I'll do what I can without it.",
        dedupe_key=f"task:{task_id}:connect:{cap}",
    )
    value = {"type": "connect", "capability": cap, "connected": False}
    await bus.get_bus().enqueue(Job(
        id=f"resume:{task_id}:connect:{cap}", user_id=user_id, kind=JobKind.RESUME_TASK,
        payload={"task_id": task_id, "value": value},
    ))


def reset_defaults() -> None:
    """Restore the built-in handlers (integrations wiring overrides "connect" with ConnectFlow)."""
    INTERRUPT_HANDLERS.clear()
    register_interrupt_handler("approval", _approval)
    register_interrupt_handler("connect", _connect_unavailable)


reset_defaults()
