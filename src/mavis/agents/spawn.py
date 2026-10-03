"""Generic worker agents created on demand by the planner (agent='spawn').

A worker is bounded by its own step and wall-clock budget. If it is exposed to a parent loop as a
tool, register that tool with `timeout_s=worker_tool_timeout_s(budget)` (MavisTool) or
`metadata={"timeout_s": ...}` (LangChain), otherwise react_loop's 45 s per-tool limit would cut the
worker off. Taint flows through the shared ToolRun: a worker that reads untrusted output taints its
caller (see react_loop).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from mavis.agents.react import react_loop
from mavis.config import get_settings
from mavis.domain.errors import BudgetExceeded
from mavis.domain.tasks import StepOutcome
from mavis.llm import models as llm
from mavis.policy.risk import UNTRUSTED_NOTE, wrap_untrusted
from mavis.tools.registry import current_run, get_registry

log = structlog.get_logger()

SPAWN_PROMPT = (
    "You are a focused worker agent. Your role: {role}. Complete only your goal, use tools when they "
    "help, and finish with a self-contained answer another agent can use without seeing your work."
)


@dataclass(frozen=True)
class Budget:
    max_steps: int = 8
    tier: llm.Tier = llm.Tier.SMART
    timeout_s: float = 240  # wall-clock bound for the whole worker run


def worker_tool_timeout_s(budget: Budget) -> float:
    """Per-tool limit for a tool that runs a worker: the worker's own bound plus a little slack."""
    return budget.timeout_s + 15


async def spawn_agent(
    user_id: int,
    role: str,
    goal: str,
    tools: list[str],
    budget: Budget = Budget(),
    context: str = "",
    *,
    tainted: bool = False,
) -> StepOutcome:
    run = current_run.get()
    if run is not None:
        cap = get_settings().spawn_max_per_step
        if run.spawned >= cap:
            log.warning("spawn.too_many", role=role, cap=cap)
            return StepOutcome(
                ok=False, error="too many workers", tainted=run.tainted,
                text=wrap_untrusted(
                    f"Too many workers: at most {cap} can start per step. Do this one in a later step "
                    "or fold it into one of the workers already running.", "spawn_agent"),
            )
        run.spawned += 1
    registry = get_registry()
    allowed = set(registry.names_for("spawn"))
    granted = [t for t in tools if t in allowed]
    dropped = sorted(set(tools) - allowed)
    if dropped:
        log.warning("spawn.tools_dropped", role=role, dropped=dropped)
    lc_tools = registry.for_agent("spawn", user_id, names=granted) if granted else []
    messages = [
        SystemMessage(f"{SPAWN_PROMPT.format(role=role)}\n\n{UNTRUSTED_NOTE}"),
        HumanMessage(f"Goal:\n{goal}\n\nContext:\n{context or '(none)'}"),
    ]
    try:
        async with asyncio.timeout(budget.timeout_s):
            result = await react_loop(
                lc_tools, messages, max_steps=budget.max_steps, tier=budget.tier, temperature=0.3,
                name=f"spawn:{role}", priority="background", fallback=True, tainted=tainted,
            )
    except TimeoutError as exc:
        raise BudgetExceeded(f"worker {role!r} ran longer than {budget.timeout_s:g}s") from exc
    return StepOutcome(
        ok=bool(result.text), text=result.text, error=None if result.text else "empty answer",
        tainted=bool(getattr(result, "tainted", False)),
    )
