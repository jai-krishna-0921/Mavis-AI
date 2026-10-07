"""Specialist = prompt + model tier + tool subset + step and time budget, run as a bounded ReAct loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from mavis.agents.react import react_loop
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.errors import BudgetExceeded
from mavis.domain.tasks import StepOutcome
from mavis.llm import models as llm
from mavis.policy.risk import UNTRUSTED_NOTE
from mavis.tools.registry import get_registry


@dataclass(frozen=True)
class Specialist:
    name: str
    description: str
    prompt: str
    tier: llm.Tier = llm.Tier.SMART
    tool_names: tuple[str, ...] = ()  # empty = every tool registered for this agent name
    max_steps: int = 12
    steps_setting: str | None = None  # a Settings field that overrides max_steps (see step_budget)
    timeout_s: float = 240  # wall-clock bound for one run (the per-tool 45 s limit does not apply to us)
    # Custom runner (user_id, instruction, context) -> StepOutcome, used instead of the ReAct loop
    # (Phase 6 Docs / DeepResearch).
    runner: Callable[[int, str, str], Awaitable[StepOutcome]] | None = None


# Seconds of a specialist's wall clock kept for the wrap-up answer once its tool time is up.
WRAP_UP_RESERVE_S = 30.0


def step_budget(spec: Specialist) -> int:
    """Tool rounds for one run: the configured value when the specialist names a setting."""
    if spec.steps_setting:
        return int(getattr(get_settings(), spec.steps_setting))
    return spec.max_steps


def wrap_up_deadline(timeout_s: float) -> float:
    """When a background loop stops calling tools and answers, leaving room inside its hard timeout."""
    return max(timeout_s - WRAP_UP_RESERVE_S, timeout_s * 0.75)


def outcome_of(result: object) -> StepOutcome:
    """A loop's result as a step outcome. A wrapped-up run (out of rounds or time) is `partial`: it
    answered from what it had gathered, so the work is kept rather than thrown away."""
    text = str(getattr(result, "text", "") or "")
    return StepOutcome(
        ok=bool(text), text=text, error=None if text else "empty answer",
        tainted=bool(getattr(result, "tainted", False)),
        partial=bool(getattr(result, "wrapped_up", False)),
    )


# Set by the orchestrator's run_step to the plan's deliverable ("message" | "pptx" | "pdf" | "docx" | ...).
current_deliverable: ContextVar[str] = ContextVar("current_deliverable", default="message")


async def run_specialist(
    spec: Specialist, user_id: int, instruction: str, context: str = "", *, tainted: bool = False
) -> StepOutcome:
    """Run one specialist. Out of tool rounds (or near its time limit) it answers from what it gathered
    (`StepOutcome.partial`); raises BudgetExceeded only past the hard time limit, and ConnectionRequired.

    Pass `tainted=True` when `context` carries third-party content. Taint read by the loop is shared
    with any enclosing loop (see react_loop), so a caller of this function inherits it.
    """
    if spec.runner is not None:
        return await spec.runner(user_id, instruction, context)
    tools = get_registry().for_agent(spec.name, user_id, names=spec.tool_names or None)
    messages = [
        SystemMessage(f"{spec.prompt}\n\n{UNTRUSTED_NOTE}\nCurrent UTC time: {timeutil.now().isoformat()}"),
        HumanMessage(f"Task:\n{instruction}\n\nContext:\n{context or '(none)'}"),
    ]
    try:
        async with asyncio.timeout(spec.timeout_s):
            result = await react_loop(
                tools, messages, max_steps=step_budget(spec), tier=spec.tier, temperature=0.2,
                name=f"specialist:{spec.name}", priority="background", fallback=True, tainted=tainted,
                wrap_up=True, deadline_s=wrap_up_deadline(spec.timeout_s),
            )
    except TimeoutError as exc:
        raise BudgetExceeded(f"specialist {spec.name} ran longer than {spec.timeout_s:g}s") from exc
    return outcome_of(result)
