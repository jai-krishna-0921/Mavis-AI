"""A small, budgeted tool-calling loop shared by chat turns, specialists and spawned workers.

Hand-rolled rather than a prebuilt agent so the step budget, error reporting, taint tracking and
ConnectionRequired propagation are explicit and testable. Every model call goes through
`llm.invoke_tools`, so the single LLM slot, deadlines, Ollama cooldown/backoff and the secondary
provider apply. Tool calls from one AI message run concurrently ("parallel tools, serialized LLM").
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool

from mavis.domain.errors import ApprovalRequired, BudgetExceeded, ConnectionRequired
from mavis.llm import models as llm
from mavis.tools.registry import ToolRun, current_run

log = structlog.get_logger(__name__)

MAX_CALLS_PER_STEP = 8  # tool calls executed from one AI message; extras are answered "skipped"
_ERROR_CHARS = 300


@dataclass
class ReactResult:
    text: str
    steps: int
    messages: list[BaseMessage] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)  # tools that ran, in call order
    queued_approvals: list[int] = field(default_factory=list)  # pending_approvals ids queued this run
    # ApprovalRequired raised by a tool outside the registry: nothing was queued or done.
    unqueued_approvals: list[ApprovalRequired] = field(default_factory=list)
    tainted: bool = False  # the model saw untrusted tool output during this run


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)


def _with_call_ids(ai: AIMessage, step: int) -> AIMessage:
    """Every tool call (valid or not) needs an id so each one can be answered with a ToolMessage."""
    calls = [dict(c) for c in ai.tool_calls]
    bad = [dict(c) for c in ai.invalid_tool_calls]
    missing = False
    for i, call in enumerate([*calls, *bad]):
        if not call.get("id"):
            call["id"] = f"call_{step}_{i}"
            missing = True
    if not missing:
        return ai
    return ai.model_copy(update={"tool_calls": calls, "invalid_tool_calls": bad})


async def react_loop(
    tools: Sequence[BaseTool],
    messages: Sequence[BaseMessage],
    max_steps: int,
    *,
    tier: llm.Tier = llm.Tier.FAST,
    temperature: float = 0.3,
    name: str = "react",
    priority: llm.Priority = "interactive",
    fallback: bool | None = None,
    tainted: bool = False,
) -> ReactResult:
    """Call the model, run the tools it asks for, repeat until it answers in text.

    Raises BudgetExceeded after more than `max_steps` tool rounds, ConnectionRequired (after the
    step's other calls settle) when a tool needs an account linked, and LLMError if the model fails.
    Pass `tainted=True` when `messages` already carry third-party content.
    """
    by_name = {t.name: t for t in tools}
    history: list[BaseMessage] = list(messages)
    # A nested loop (a tool that spawns a worker) shares its parent's run: taint flows both ways.
    run = current_run.get() or ToolRun()
    run.tainted = run.tainted or tainted
    first_approval = len(run.queued_approvals)
    token = current_run.set(run)
    tools_called: list[str] = []
    unqueued: list[ApprovalRequired] = []
    steps = 0
    try:
        while True:
            ai = await llm.invoke_tools(history, list(tools), tier=tier, temperature=temperature,
                                        name=name, priority=priority, fallback=fallback)
            ai = _with_call_ids(ai, steps)
            history.append(ai)
            calls, bad = ai.tool_calls, ai.invalid_tool_calls
            if not calls and not bad:
                return ReactResult(
                    text=_text_of(ai.content).strip(), steps=steps, messages=history,
                    tools_called=tools_called, queued_approvals=run.queued_approvals[first_approval:],
                    unqueued_approvals=unqueued, tainted=run.tainted,
                )
            steps += 1
            if steps > max_steps:
                raise BudgetExceeded(f"more than {max_steps} tool rounds")
            history.extend(await _run_step(calls, bad, by_name, tools_called, unqueued))
            run.end_step()
    finally:
        current_run.reset(token)


async def _run_step(
    calls: list[Any],
    bad: list[Any],
    by_name: dict[str, BaseTool],
    tools_called: list[str],
    unqueued: list[ApprovalRequired],
) -> list[ToolMessage]:
    """Run one AI message's tool calls concurrently; answer every call id, in call order."""
    runnable = calls[:MAX_CALLS_PER_STEP]
    for call in runnable:
        if call["name"] in by_name:
            tools_called.append(call["name"])
    results = await asyncio.gather(*(_run_call(c, by_name) for c in runnable), return_exceptions=True)

    out: list[ToolMessage] = []
    connection: ConnectionRequired | None = None
    for call, res in zip(runnable, results, strict=True):
        if isinstance(res, str):
            content = res
        elif isinstance(res, ConnectionRequired):
            connection = connection or res
            content = "Needs an account connected first. The user is being asked to connect it."
        elif isinstance(res, ApprovalRequired):
            unqueued.append(res)
            content = "This needs the user's approval and has NOT been done."
        elif isinstance(res, Exception):
            log.warning("react.tool_error", tool=call["name"], error_type=type(res).__name__)
            content = f"Tool error: {type(res).__name__}: {str(res)[:_ERROR_CHARS]}"
        else:  # CancelledError and other BaseExceptions are not ours to swallow
            raise res
        out.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
    for call in calls[MAX_CALLS_PER_STEP:]:
        out.append(ToolMessage(
            content=f"Skipped: at most {MAX_CALLS_PER_STEP} tool calls per step. "
                    "Call it again if still needed.",
            tool_call_id=call["id"], name=call["name"],
        ))
    for call in bad:
        name = call.get("name") or "unknown"
        error = str(call.get("error") or "invalid arguments")[:_ERROR_CHARS]
        log.warning("react.malformed_tool_call", tool=name)
        out.append(ToolMessage(
            content=f"Malformed tool call for {name!r}: {error}. Call it again with valid JSON arguments.",
            tool_call_id=call["id"], name=name,
        ))
    if connection is not None:
        raise connection
    return out


async def _run_call(call: Any, by_name: dict[str, BaseTool]) -> str:
    tool = by_name.get(call["name"])
    if tool is None:
        return f"Unknown tool {call['name']!r}. Available: {', '.join(by_name) or 'none'}."
    args = call.get("args")
    if not isinstance(args, dict):
        return f"Malformed tool call for {call['name']!r}: arguments must be a JSON object."
    return str(await tool.ainvoke(args))
