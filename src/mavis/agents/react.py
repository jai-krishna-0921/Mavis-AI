"""A small, budgeted tool-calling loop shared by chat turns, specialists and spawned workers.

Hand-rolled rather than a prebuilt agent so the step budget, error reporting, taint tracking and
ConnectionRequired propagation are explicit and testable. Every model call goes through
`llm.invoke_tools`, so the single LLM slot, deadlines, Ollama cooldown/backoff and the secondary
provider apply. Tool calls from one AI message run concurrently ("parallel tools, serialized LLM").
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool

from mavis.config import get_settings
from mavis.domain.errors import ApprovalRequired, BudgetExceeded, ConnectionRequired, LLMError
from mavis.llm import models as llm
from mavis.policy.risk import wrap_untrusted
from mavis.tools.registry import ToolRun, current_run

log = structlog.get_logger(__name__)

MAX_CALLS_PER_STEP = 8  # tool calls executed from one AI message; extras are answered "skipped"
_ERROR_CHARS = 300
WRAP_UP_NOTE = (
    "[System note, not from the user] Time is up for looking things up. Do not call any more tools. "
    "Answer now with what you have, and say briefly if you could not check something."
)


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
    read_untrusted: bool = False  # a tool in THIS loop returned third-party text (not inherited taint)
    wrapped_up: bool = False  # wrap_up forced a final answer (step budget or deadline reached)


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
    self_tainted: bool | None = None,
    user_words: str = "",
    wrap_up: bool = False,
    deadline_s: float | None = None,
    tool_timeout_s: float | None = None,
    wrap_up_timeout_s: float | None = None,
    digest_on_failed_wrap_up: bool = False,
) -> ReactResult:
    """Call the model, run the tools it asks for, repeat until it answers in text.

    Raises BudgetExceeded after more than `max_steps` tool rounds, ConnectionRequired (after the
    step's other calls settle) when a tool needs an account linked, and LLMError if the model fails.
    Pass `tainted=True` when `messages` already carry third-party content. A chat turn also passes
    `self_tainted` (what can steer this turn: the previous reply, hook context) and `user_words` (the
    user's message), by which self-only actions are judged instead (ToolRun.self_tainted).

    `wrap_up=True` (interactive turns): instead of raising BudgetExceeded, once `max_steps` tool
    rounds ran or `deadline_s` (seconds from now) has passed, the model is asked once more to answer
    from what it has; any tool calls in that answer are ignored (`ReactResult.wrapped_up`). Tool
    calls are also cut off at the deadline. `tool_timeout_s` overrides the settings default for
    tools without their own `timeout_s`.

    Wrap-up robustness (background loops): with `deadline_s`, every model call before the wrap-up is cut
    off at the deadline (the loop then wraps up instead of running into its caller's hard timeout). The
    wrap-up call binds NO tools, so a model cannot answer it with tool calls only, and it is bounded by
    `wrap_up_timeout_s`. With `digest_on_failed_wrap_up`, a wrap-up that fails or times out, or a model
    error after some tool rounds, returns a plain digest of the tool results gathered so far instead of
    raising and discarding them (never for chat: raw tool output must not become a reply).
    """
    by_name = {t.name: t for t in tools}
    history: list[BaseMessage] = list(messages)
    # A nested loop (a tool that spawns a worker) shares its parent's run: taint flows both ways.
    parent = current_run.get()
    run = parent or ToolRun()
    run.tainted = run.tainted or tainted
    if parent is None:
        run.self_tainted = self_tainted
        run.user_words = user_words
    first_approval = len(run.queued_approvals)
    first_read = run.untrusted_reads
    token = current_run.set(run)
    tools_called: list[str] = []
    unqueued: list[ApprovalRequired] = []
    steps = 0
    end = None if deadline_s is None else time.monotonic() + deadline_s
    out_of_time = False
    try:
        while True:
            final = wrap_up and steps > 0 and (
                out_of_time or steps >= max_steps or (end is not None and time.monotonic() >= end)
            )
            if final:
                try:
                    async with asyncio.timeout(wrap_up_timeout_s):
                        ai = await llm.invoke_tools([*history, HumanMessage(WRAP_UP_NOTE)], [], tier=tier,
                                                    temperature=temperature, name=name, priority=priority,
                                                    fallback=fallback)
                except (TimeoutError, LLMError) as exc:
                    if not digest_on_failed_wrap_up:
                        raise
                    log.warning("react.wrap_up_failed", name=name, error_type=type(exc).__name__)
                    ai = AIMessage(content=gathered_digest(history[len(messages):]))
            else:
                try:
                    # the first call is never cut (there is nothing to wrap up yet)
                    async with asyncio.timeout(_left(end) if wrap_up and steps > 0 else None):
                        ai = await llm.invoke_tools(history, list(tools), tier=tier, temperature=temperature,
                                                    name=name, priority=priority, fallback=fallback)
                except TimeoutError:
                    if not (wrap_up and steps > 0 and end is not None and time.monotonic() >= end):
                        raise
                    log.info("react.model_call_cut_at_deadline", name=name, steps=steps)
                    out_of_time = True
                    continue
                except LLMError as exc:
                    # a background loop whose model fails after some rounds keeps what it gathered, as
                    # on running out of budget; with nothing gathered yet (or in chat) the error stands
                    digest = gathered_digest(history[len(messages):]) if digest_on_failed_wrap_up else ""
                    if not (wrap_up and steps > 0 and digest):
                        raise
                    log.warning("react.model_failed_mid_loop", name=name, steps=steps,
                                error_type=type(exc).__name__)
                    history.append(AIMessage(content=digest))
                    return ReactResult(
                        text=digest, steps=steps, messages=history, tools_called=tools_called,
                        queued_approvals=run.queued_approvals[first_approval:], unqueued_approvals=unqueued,
                        tainted=run.tainted, wrapped_up=True, read_untrusted=run.untrusted_reads > first_read,
                    )
            ai = _sanitized(ai, steps)
            history.append(ai)
            calls, bad = ai.tool_calls, ai.invalid_tool_calls
            if final or (not calls and not bad):
                if final:
                    log.info("react.wrapped_up", name=name, steps=steps, ignored_calls=len(calls) + len(bad))
                return ReactResult(
                    text=_text_of(ai.content).strip(), steps=steps, messages=history,
                    tools_called=tools_called, queued_approvals=run.queued_approvals[first_approval:],
                    unqueued_approvals=unqueued, tainted=run.tainted, wrapped_up=final,
                    read_untrusted=run.untrusted_reads > first_read,
                )
            steps += 1
            if steps > max_steps:
                raise BudgetExceeded(f"more than {max_steps} tool rounds")
            messages_out, connection = await _run_step(
                calls, bad, by_name, tools_called, unqueued, default_timeout=tool_timeout_s, end=end
            )
            if connection is not None:
                connection.partial_messages = messages_out
                connection.queued_approvals = run.queued_approvals[first_approval:]
                raise connection
            history.extend(messages_out)
            run.end_step()
            if parent is None:
                run.spawned = 0  # the per-step worker cap counts the outermost loop's steps
    finally:
        current_run.reset(token)


GATHERED_HEAD = "I ran out of time before writing this up. What I found so far:"
_DIGEST_CHARS = 600


def gathered_digest(messages: Sequence[BaseMessage]) -> str:
    """The tool results a loop gathered, as plain text (its wrap-up answer failed). Empty if none."""
    lines = [f"- {m.name or 'tool'}: {_text_of(m.content)[:_DIGEST_CHARS]}" for m in messages
             if isinstance(m, ToolMessage) and _text_of(m.content).strip()
             and not _text_of(m.content).startswith(("Tool error", "Unknown tool", "Skipped", "Malformed"))]
    return f"{GATHERED_HEAD}\n" + "\n".join(lines) if lines else ""


def _left(end: float | None) -> float | None:
    """Seconds until a monotonic deadline (None: no deadline), never below a small floor."""
    return None if end is None else max(end - time.monotonic(), 0.05)


async def _run_step(
    calls: list[Any],
    bad: list[Any],
    by_name: dict[str, BaseTool],
    tools_called: list[str],
    unqueued: list[ApprovalRequired],
    *,
    default_timeout: float | None = None,
    end: float | None = None,
) -> tuple[list[ToolMessage], ConnectionRequired | None]:
    """Run one AI message's tool calls concurrently; answer every call id, in call order.

    Returns the ToolMessages and the first ConnectionRequired (raised by the caller once all calls
    have settled). `tools_called` gains only the calls that actually ran to a result.
    """
    runnable = calls[:MAX_CALLS_PER_STEP]
    results = await asyncio.gather(
        *(_run_call(c, by_name, default_timeout, end) for c in runnable), return_exceptions=True
    )

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


def _timeout_for(tool: BaseTool, default: float | None = None, end: float | None = None) -> float | None:
    """`get_settings().tool_timeout_s` (or `default`), unless the tool sets `metadata["timeout_s"]`
    (<= 0: none). Long-running tools such as a spawned worker loop must raise their own limit this way.
    Never past `end` (a monotonic deadline), with a small floor so a call is not dead on arrival."""
    value = (tool.metadata or {}).get("timeout_s")
    if value is None:
        value = get_settings().tool_timeout_s if default is None else default
    limit = float(value) if float(value) > 0 else None
    if end is not None:
        left = max(end - time.monotonic(), 1.0)
        limit = left if limit is None else min(limit, left)
    return limit


async def _run_call(
    call: Any, by_name: dict[str, BaseTool], default_timeout: float | None = None, end: float | None = None
) -> tuple[bool, str]:
    """(ran, content): ran is False when the call never reached a tool or the tool timed out."""
    name = call["name"]
    tool = by_name.get(name)
    if tool is None:
        return False, f"Unknown tool {name!r}. Available: {', '.join(by_name) or 'none'}."
    args = call.get("args")
    if not isinstance(args, dict):
        return False, f"Malformed tool call for {name!r}: arguments must be a JSON object."
    limit = _timeout_for(tool, default_timeout, end)
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
