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

from mavis.config import get_settings
from mavis.domain.errors import ApprovalRequired, BudgetExceeded, ConnectionRequired
from mavis.llm import models as llm
from mavis.policy.risk import wrap_untrusted
from mavis.tools.registry import ToolRun, current_run

log = structlog.get_logger(__name__)

MAX_CALLS_PER_STEP = 8  # tool calls executed from one AI message; extras are answered "skipped"
_ERROR_CHARS = 300


@dataclass
class ReactResult:
    text: str
    steps: int
    messages: list[BaseMessage] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)  # tools that ran to a result, in call order
    queued_approvals: list[int] = field(default_factory=list)  # pending_approvals ids queued this run
    # ApprovalRequired raised by a tool outside the registry: nothing was queued or done.
    unqueued_approvals: list[ApprovalRequired] = field(default_factory=list)
    tainted: bool = False  # the model saw untrusted tool output during this run


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)


def _sanitized(ai: AIMessage, step: int) -> AIMessage:
    """Make the AI message safe to send back to the provider on the next turn.

    Every tool call (valid or not) gets an id so each one can be answered with a ToolMessage, and
    malformed calls carry `args="{}"`: echoing the broken argument string back makes OpenAI-style
    servers (Ollama included) reject the whole request with a 400. The parse error is kept in
    `error` and reported to the model in that call's ToolMessage.
    """
    calls = [dict(c) for c in ai.tool_calls]
    bad = [{**c, "args": "{}"} for c in ai.invalid_tool_calls]
    missing = False
    for i, call in enumerate([*calls, *bad]):
        if not call.get("id"):
            call["id"] = f"call_{step}_{i}"
            missing = True
    if not missing and not bad:
        return ai
    extra = {k: v for k, v in ai.additional_kwargs.items() if k != "tool_calls"}  # raw provider copy
    return ai.model_copy(update={"tool_calls": calls, "invalid_tool_calls": bad, "additional_kwargs": extra})


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
            ai = _sanitized(ai, steps)
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
            messages_out, connection = await _run_step(calls, bad, by_name, tools_called, unqueued)
            if connection is not None:
                connection.partial_messages = messages_out
                connection.queued_approvals = run.queued_approvals[first_approval:]
                raise connection
            history.extend(messages_out)
            run.end_step()
    finally:
        current_run.reset(token)


async def _run_step(
    calls: list[Any],
    bad: list[Any],
    by_name: dict[str, BaseTool],
    tools_called: list[str],
    unqueued: list[ApprovalRequired],
) -> tuple[list[ToolMessage], ConnectionRequired | None]:
    """Run one AI message's tool calls concurrently; answer every call id, in call order.

    Returns the ToolMessages and the first ConnectionRequired (raised by the caller once all calls
    have settled). `tools_called` gains only the calls that actually ran to a result.
    """
    runnable = calls[:MAX_CALLS_PER_STEP]
    results = await asyncio.gather(*(_run_call(c, by_name) for c in runnable), return_exceptions=True)

    out: list[ToolMessage] = []
    connection: ConnectionRequired | None = None
    for call, res in zip(runnable, results, strict=True):
        if isinstance(res, tuple):
            ran, content = res
            if ran:
                tools_called.append(call["name"])
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
    return out, connection


def _timeout_for(tool: BaseTool) -> float | None:
    """`get_settings().tool_timeout_s`, unless the tool sets `metadata["timeout_s"]` (<= 0: none).
    Long-running tools such as a spawned worker loop must raise their own limit this way."""
    value = (tool.metadata or {}).get("timeout_s")
    limit = get_settings().tool_timeout_s if value is None else float(value)
    return limit if limit > 0 else None


async def _run_call(call: Any, by_name: dict[str, BaseTool]) -> tuple[bool, str]:
    """(ran, content): ran is False when the call never reached a tool or the tool timed out."""
    name = call["name"]
    tool = by_name.get(name)
    if tool is None:
        return False, f"Unknown tool {name!r}. Available: {', '.join(by_name) or 'none'}."
    args = call.get("args")
    if not isinstance(args, dict):
        return False, f"Malformed tool call for {name!r}: arguments must be a JSON object."
    limit = _timeout_for(tool)
    deadline = asyncio.timeout(limit)
    try:
        async with deadline:
            return True, str(await tool.ainvoke(args))
    except TimeoutError:
        if not deadline.expired():
            raise  # the tool's own timeout: reported like any other tool error
        log.warning("react.tool_timeout", tool=name, timeout_s=limit)
        return False, wrap_untrusted(
            f"Tool error: TimeoutError: {name} took longer than {limit:g}s and was stopped. "
            "Do not retry it in this turn.", name,
        )
