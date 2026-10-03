"""Specialist = prompt + model tier + tool subset + step and time budget, run as a bounded ReAct loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from mavis.agents.react import react_loop
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
    timeout_s: float = 240  # wall-clock bound for one run (the per-tool 45 s limit does not apply to us)
    # Custom runner (user_id, instruction, context) -> StepOutcome, used instead of the ReAct loop
    # (Phase 6 Docs / DeepResearch).
    runner: Callable[[int, str, str], Awaitable[StepOutcome]] | None = None


# Set by the orchestrator's run_step to the plan's deliverable ("message" | "pptx" | "pdf" | "docx" | ...).
current_deliverable: ContextVar[str] = ContextVar("current_deliverable", default="message")


async def run_specialist(
    spec: Specialist, user_id: int, instruction: str, context: str = "", *, tainted: bool = False
) -> StepOutcome:
    """Run one specialist. Raises BudgetExceeded (steps or time) and ConnectionRequired.

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
                tools, messages, max_steps=spec.max_steps, tier=spec.tier, temperature=0.2,
                name=f"specialist:{spec.name}", priority="background", fallback=True, tainted=tainted,
            )
    except TimeoutError as exc:
        raise BudgetExceeded(f"specialist {spec.name} ran longer than {spec.timeout_s:g}s") from exc
    return StepOutcome(
        ok=bool(result.text), text=result.text, error=None if result.text else "empty answer",
        tainted=bool(getattr(result, "tainted", False)),
    )
