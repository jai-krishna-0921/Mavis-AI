"""Tool registry: one place that knows every tool's risk, capability and audience.

Agents never see raw functions. `for_agent()` / `select()` hand out LangChain tools
whose wrapper enforces, in order: capability (ConnectionRequired), approval for
risky actions (queues a pending_approvals row instead of running), truncation,
and <untrusted> wrapping of third-party output.

Taint: inside a tool loop (`current_run`, set by `agents.react.react_loop`), once the model has
seen any untrusted_output result, trusted-writing tools follow their `on_taint` policy (queue for
approval, or run a reduced-trust `tainted_fn`), and standing rules no longer auto-approve.
"""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any

import structlog
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from mavis.config import get_settings
from mavis.domain.errors import ActionFailed, ApprovalRequired, ConnectionRequired
from mavis.domain.policy import Capability, RiskClass
from mavis.policy.risk import truncate, wrap_untrusted
from mavis.store.db import utcnow
from mavis.store.repo import approvals, audit, policy_rules

log = structlog.get_logger()

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that say nothing about which tool fits ("the", "user", "a"): ignored by select()'s overlap score.
_STOPWORDS = frozenset(
    "a an and are as at be by can do for from i in is it me my of on or s so the their them they this "
    "to up us user we what when with you your".split()
)
_SUFFIXES = ("ings", "ing", "ers", "er", "ed", "es", "s")


def _terms(text: str) -> set[str]:
    """Crude stems for select()'s overlap score: "reminder" ~ "remind", "emails" ~ "email"."""
    out = set()
    for word in _WORD_RE.findall(text.lower()):
        if word in _STOPWORDS:
            continue
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
        out.add(word[:-1] if word.endswith("e") and len(word) > 3 else word)
    return out


NEVER_AUTO_APPROVE = frozenset({"add_policy_rule", "forget"})
_PREVIEW_IN_RESULT_CHARS = 500

# Serialises "find open approval, else create" so identical parallel tool calls queue one approval.
_queue_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _queue_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _queue_locks.get(loop)
    if lock is None:
        lock = _queue_locks[loop] = asyncio.Lock()
    return lock

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
current_task_id: ContextVar[int | None] = ContextVar("current_task_id", default=None)


class TaintPolicy(StrEnum):
    """What a tool does once the model has seen third-party (untrusted) output in this run."""

    ALLOW = "allow"  # unchanged: reads, drafts, anything that cannot plant trusted state
    APPROVE = "approve"  # queue for the user's approval instead of running
    DOWNGRADE = "downgrade"  # run `tainted_fn` instead (same args, third-party trust)


@dataclass
class ToolRun:
    """Per tool-loop state shared between the loop and the registry (via `current_run`).

    `tainted` gates trusted writes. It flips only between model steps (`end_step`), because the
    model cannot have been steered by output it has not seen yet: a tool call issued in the same
    AI message as `mail_read` was decided before the email was read.
    """

    tainted: bool = False
    untrusted_seen: bool = False  # an untrusted_output tool returned during the current step
    queued_approvals: list[int] = field(default_factory=list)
    spawned: int = 0  # workers started in the current outermost model step (reset by react_loop)
    memo: dict[str, Any] = field(default_factory=dict)  # per-run cache for `prepare` lookups (file metadata)

    def end_step(self) -> None:
        self.tainted = self.tainted or self.untrusted_seen
        self.untrusted_seen = False


current_run: ContextVar[ToolRun | None] = ContextVar("current_run", default=None)


def _run_tainted() -> bool:
    run = current_run.get()
    return run is not None and run.tainted


ToolFn = Callable[[int, Any], Awaitable[str | dict | list]]
CapabilityCheck = Callable[[int, Capability], Awaitable[bool]]


@dataclass(frozen=True)
class ToolContext:
    """What a context-aware tool (Phases 5-6) needs beyond its arguments."""

    user_id: int
    timezone: str = "UTC"
    task_id: int | None = None


async def tool_context(user_id: int) -> ToolContext:
    from mavis.store.repo import users

    try:
        tz = (await users.get(user_id)).timezone
    except SQLAlchemyError:  # unknown user or no DB (unit tests): fall back to UTC
        tz = "UTC"
    return ToolContext(user_id=user_id, timezone=tz, task_id=current_task_id.get())


def contextual(fn: Callable[[ToolContext, Any], Awaitable[str | dict | list]]) -> ToolFn:
    """Adapt an `async fn(ctx, args)` to the registry's `async fn(user_id, args)` signature."""

    async def wrapped(user_id: int, args: Any) -> str | dict | list:
        return await fn(await tool_context(user_id), args)

    wrapped.__name__ = getattr(fn, "__name__", "contextual")
    return wrapped


@dataclass(frozen=True)
class MavisTool:
    name: str
    description: str
    args_model: type[BaseModel]
    risk: RiskClass
    fn: ToolFn
    agents: frozenset[str]
    requires: Capability | None = None
    preview: Callable[..., str] | None = None
    untrusted_output: bool = False
    priority: int = 50  # higher = more likely to be offered when tools must be trimmed
    risk_fn: Callable[[BaseModel], RiskClass] | None = None  # argument-dependent risk
    preview_needs_ctx: bool = False  # True => preview(args, ctx: ToolContext)
    on_taint: TaintPolicy = TaintPolicy.ALLOW
    tainted_fn: ToolFn | None = None  # required for TaintPolicy.DOWNGRADE
    timeout_s: float | None = None  # react_loop per-call limit; None = tool_timeout_s, <= 0 = none
    # Async pre-step with network access (file metadata, allowlists), run by invoke() before the taint and
    # approval checks; never by execute_approved (the user already saw the preview and said yes).
    prepare: PrepareFn | None = None

    def effective_risk(self, args: BaseModel) -> RiskClass:
        return self.risk_fn(args) if self.risk_fn is not None else self.risk

    def render_preview(self, args: BaseModel, ctx: ToolContext | None = None) -> str:
        if self.preview is not None:
            if self.preview_needs_ctx:
                return self.preview(args, ctx or ToolContext(user_id=0))
            return self.preview(args)
        return f"{self.name} {args.model_dump_json()}"


@dataclass(frozen=True)
class Prepared:
    """What a tool's async pre-step decided, before taint and approval checks (Workspace spec 4.1).

    `risk` can only raise the call's risk (an escalation to OUTWARD or DESTRUCTIVE); `refusal` is returned
    to the model as the tool result and nothing runs or queues; `note` is appended to the approval preview
    (facts the pre-step verified, such as the real file title, never model-written text)."""

    risk: RiskClass | None = None
    refusal: str | None = None
    note: str | None = None


PrepareFn = Callable[[ToolContext, BaseModel], Awaitable[Prepared]]
_RISK_RANK = {
    RiskClass.READ: 0, RiskClass.WRITE_SELF: 1, RiskClass.OUTWARD: 2, RiskClass.SPEND: 3,
    RiskClass.DESTRUCTIVE: 4,
}


def higher_risk(a: RiskClass, b: RiskClass) -> RiskClass:
    return a if _RISK_RANK[a] >= _RISK_RANK[b] else b


async def _always_available(user_id: int, capability: Capability) -> bool:
    return True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, MavisTool] = {}
        self.capability_check: CapabilityCheck = _always_available
        self.capability_reason: Callable[[Capability], str] = lambda c: f"use your {c.value}"
        self.available: Callable[[MavisTool], bool] = lambda t: True

    # --- catalogue -------------------------------------------------------------

    def register(self, tool: MavisTool) -> None:
        if not isinstance(tool.risk, RiskClass):
            raise ValueError(f"tool {tool.name!r} needs a RiskClass risk")
        if tool.on_taint is TaintPolicy.DOWNGRADE and tool.tainted_fn is None:
            raise ValueError(f"tool {tool.name!r} has on_taint=DOWNGRADE but no tainted_fn")
        if not _NAME_RE.match(tool.name):
            raise ValueError(f"invalid tool name {tool.name!r}; use [a-zA-Z0-9_-]")
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> MavisTool:
        return self._tools[name]

    def names_for(self, agent: str) -> list[str]:
        return [t.name for t in self._tools.values() if agent in t.agents]

    def for_agent(self, agent: str, user_id: int, names: Iterable[str] | None = None) -> list[BaseTool]:
        wanted = set(names) if names is not None else None
        return [
            self._as_langchain(t, user_id)
            for t in self._tools.values()
            if agent in t.agents and self.available(t) and (wanted is None or t.name in wanted)
        ]

    def select(
        self, agent: str, user_id: int, query: str, limit: int = 8, always: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> list[BaseTool]:
        """Top-`limit` tools for an agent: `always` first, the rest by word overlap then priority.
        `exclude` names are never offered, whatever their agents."""
        words = _terms(query)
        banned = set(exclude)
        candidates = [t for t in self._tools.values()
                      if agent in t.agents and self.available(t) and t.name not in banned]
        pinned: list[MavisTool] = []
        for name in always:
            t = self._tools.get(name)
            if t is not None and t in candidates and t not in pinned:
                pinned.append(t)

        def score(t: MavisTool) -> tuple[int, int]:
            vocab = _terms(f"{t.name.replace('_', ' ')} {t.description}")
            return (len(words & vocab), t.priority)

        rest = sorted((t for t in candidates if t not in pinned), key=score, reverse=True)
        chosen = (pinned + rest)[: max(limit, len(pinned))]
        return [self._as_langchain(t, user_id) for t in chosen]

    # --- execution -------------------------------------------------------------

    async def _require_capability(self, tool: MavisTool, user_id: int) -> None:
        if tool.requires is not None and not await self.capability_check(user_id, tool.requires):
            raise ConnectionRequired(tool.requires, self.capability_reason(tool.requires))

    async def _prepared(self, tool: MavisTool, user_id: int, args: BaseModel) -> Prepared:
        """The call's effective risk (always set) or a refusal. A failing pre-step fails closed."""
        risk = tool.effective_risk(args)
        if tool.prepare is None:
            return Prepared(risk=risk)
        try:
            prepared = await tool.prepare(await tool_context(user_id), args)
        except (ApprovalRequired, ConnectionRequired):
            raise
        except Exception as exc:  # noqa: BLE001 - unknown means the user decides
            log.warning("tool.prepare_failed", tool=tool.name, error_type=type(exc).__name__)
            return Prepared(risk=higher_risk(risk, RiskClass.OUTWARD))
        if prepared.refusal is not None:
            log.info("tool.refused_before_approval", tool=tool.name)
            return Prepared(risk=risk, refusal=prepared.refusal)
        escalated = higher_risk(risk, prepared.risk) if prepared.risk is not None else risk
        return Prepared(risk=escalated, note=prepared.note)

    async def invoke(self, tool: MavisTool, user_id: int, args: BaseModel) -> str:
        # Capability first: the user is asked to connect BEFORE being asked to approve.
        await self._require_capability(tool, user_id)
        payload = args.model_dump(mode="json")
        prepared = await self._prepared(tool, user_id, args)
        if prepared.refusal is not None:
            return prepared.refusal  # refused before approval: nothing runs and nothing is queued
        risk = prepared.risk or tool.effective_risk(args)
        note = f"\n{prepared.note}" if prepared.note else ""
        tainted = _run_tainted()
        if tainted and tool.on_taint is TaintPolicy.APPROVE:
            log.info("tool.taint_needs_approval", tool=tool.name)
            preview = tool.render_preview(args, await tool_context(user_id)) + note
            raise ApprovalRequired(tool.name, preview, payload)
        if risk.needs_approval:
            # Standing rules may waive approval for OUTWARD tools only; SPEND and DESTRUCTIVE always queue.
            # Never after untrusted output: an email must not ride a rule like "always allow X".
            auto = (
                not tainted
                and risk is RiskClass.OUTWARD
                and tool.name not in NEVER_AUTO_APPROVE
                and await policy_rules.matches(user_id, tool.name, payload)
            )
            if not auto:
                preview = tool.render_preview(args, await tool_context(user_id)) + note
                raise ApprovalRequired(tool.name, preview, payload)
        if tainted and tool.on_taint is TaintPolicy.DOWNGRADE and tool.tainted_fn is not None:
            log.info("tool.taint_downgraded", tool=tool.name)
            return await self._run(tool, user_id, args, actor="agent", fn=tool.tainted_fn)
        return await self._run(tool, user_id, args, actor="agent")

    async def execute_approved(self, approval_id: int) -> str:
        """Run a tool the user explicitly approved. Bypasses the approval check only."""
        approval = await approvals.get(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        tool = self.get(approval.tool)
        args = tool.args_model.model_validate(approval.arguments)
        await self._require_capability(tool, approval.user_id)
        # The action runs on behalf of the task that held the approval (tools may read its taint).
        task_token = current_task_id.set(approval.task_id)
        try:
            # raise_errors: a failed approved action must surface as an exception, never as a result
            # string, so the caller records FAILED instead of EXECUTED.
            return await self._run(tool, approval.user_id, args, actor="user_approved", raise_errors=True)
        finally:
            current_task_id.reset(task_token)

    async def _run(
        self,
        tool: MavisTool,
        user_id: int,
        args: BaseModel,
        actor: str,
        fn: ToolFn | None = None,
        *,
        raise_errors: bool = False,
    ) -> str:
        token = current_user_id.set(user_id)
        detail: dict[str, Any] = {"args": args.model_dump(mode="json")}
        if fn is not None:
            detail["tainted"] = True
        audited = tool.effective_risk(args) is not RiskClass.READ
        run = current_run.get()
        failed = False
        try:
            out = await (fn or tool.fn)(user_id, args)
        except (ApprovalRequired, ConnectionRequired):
            raise
        except ActionFailed as exc:
            log.warning("tool.action_failed", tool=tool.name)
            if audited:
                await audit.record(
                    user_id, actor=actor, action=tool.name, detail={**detail, "outcome": "error"}
                )
            if raise_errors:
                raise
            out = str(exc)  # the model gets the sentence as the tool result (wrapped below if untrusted)
            failed = True
        except Exception as exc:
            log.warning("tool.failed", tool=tool.name, error_type=type(exc).__name__)
            if audited:
                await audit.record(
                    user_id, actor=actor, action=tool.name, detail={**detail, "outcome": "error"}
                )
            if tool.untrusted_output and not raise_errors:
                # Third-party error text must never reach the model unwrapped.
                if run is not None:
                    run.untrusted_seen = True
                return wrap_untrusted(truncate(f"Tool error: {exc}"), tool.name)
            raise
        finally:
            current_user_id.reset(token)
        text = out if isinstance(out, str) else json.dumps(out, default=str, ensure_ascii=False)
        text = truncate(text)
        if audited and not failed:
            await audit.record(user_id, actor=actor, action=tool.name, detail={**detail, "outcome": "ok"})
        if not tool.untrusted_output:
            return text
        if run is not None:
            run.untrusted_seen = True
        return wrap_untrusted(text, tool.name)

    def _as_langchain(self, tool: MavisTool, user_id: int) -> BaseTool:
        async def _call(**kwargs: Any) -> str:
            args = tool.args_model.model_validate(kwargs)
            try:
                return await self.invoke(tool, user_id, args)
            except ApprovalRequired as req:
                task_id = current_task_id.get()
                async with _queue_lock():
                    existing = await approvals.find_open(user_id, task_id, tool.name, req.arguments)
                    if existing is not None:
                        approval_id = existing.id
                    else:
                        approval_id = await approvals.create(
                            user_id=user_id,
                            task_id=task_id,
                            tool=tool.name,
                            arguments=req.arguments,
                            preview=req.preview,
                            expires_at=utcnow() + timedelta(hours=get_settings().approval_ttl_hours),
                        )
                log.info("tool.queued_for_approval", tool=tool.name, approval_id=approval_id)
                run = current_run.get()
                if run is not None and approval_id not in run.queued_approvals:
                    run.queued_approvals.append(approval_id)
                shown = wrap_untrusted(truncate(req.preview, _PREVIEW_IN_RESULT_CHARS), "approval_preview")
                return (
                    f"QUEUED_FOR_APPROVAL #{approval_id}: {shown}\n"
                    "This has NOT been done yet. Tell the user it is ready and waiting for their OK."
                )

        return StructuredTool.from_function(
            coroutine=_call, name=tool.name, description=tool.description, args_schema=tool.args_model,
            metadata=None if tool.timeout_s is None else {"timeout_s": tool.timeout_s},
        )


_REGISTRY: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        from mavis.tools import load_builtin_tools

        reg = ToolRegistry()
        load_builtin_tools(reg)
        _REGISTRY = reg
    return _REGISTRY
