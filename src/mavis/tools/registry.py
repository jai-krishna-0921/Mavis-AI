"""Tool registry: one place that knows every tool's risk, capability and audience.

Agents never see raw functions. `for_agent()` / `select()` hand out LangChain tools
whose wrapper enforces, in order: capability (ConnectionRequired), approval for
risky actions (queues a pending_approvals row instead of running), truncation,
and <untrusted> wrapping of third-party output.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from mavis.config import get_settings
from mavis.domain.errors import ApprovalRequired, ConnectionRequired
from mavis.domain.policy import Capability, RiskClass
from mavis.policy.risk import truncate, wrap_untrusted
from mavis.store.db import utcnow
from mavis.store.repo import approvals, audit, policy_rules

log = structlog.get_logger()

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_WORD_RE = re.compile(r"[a-z0-9]+")
NEVER_AUTO_APPROVE = frozenset({"add_policy_rule", "forget"})

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
current_task_id: ContextVar[int | None] = ContextVar("current_task_id", default=None)

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
    except Exception:  # noqa: BLE001 - unknown user in a unit test: fall back to UTC
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

    def effective_risk(self, args: BaseModel) -> RiskClass:
        return self.risk_fn(args) if self.risk_fn is not None else self.risk

    def render_preview(self, args: BaseModel, ctx: ToolContext | None = None) -> str:
        if self.preview is not None:
            if self.preview_needs_ctx:
                return self.preview(args, ctx or ToolContext(user_id=0))
            return self.preview(args)
        return f"{self.name} {args.model_dump_json()}"


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
        self, agent: str, user_id: int, query: str, limit: int = 8, always: Iterable[str] = ()
    ) -> list[BaseTool]:
        """Top-`limit` tools for an agent: `always` first, the rest by word overlap then priority."""
        words = set(_WORD_RE.findall(query.lower()))
        candidates = [t for t in self._tools.values() if agent in t.agents and self.available(t)]
        pinned = [t for name in always if (t := self._tools.get(name)) is not None and t in candidates]

        def score(t: MavisTool) -> tuple[int, int]:
            vocab = set(_WORD_RE.findall(f"{t.name.replace('_', ' ')} {t.description}".lower()))
            return (len(words & vocab), t.priority)

        rest = sorted((t for t in candidates if t not in pinned), key=score, reverse=True)
        chosen = (pinned + rest)[: max(limit, len(pinned))]
        return [self._as_langchain(t, user_id) for t in chosen]

    # --- execution -------------------------------------------------------------

    async def _require_capability(self, tool: MavisTool, user_id: int) -> None:
        if tool.requires is not None and not await self.capability_check(user_id, tool.requires):
            raise ConnectionRequired(tool.requires, self.capability_reason(tool.requires))

    async def invoke(self, tool: MavisTool, user_id: int, args: BaseModel) -> str:
        # Capability first: the user is asked to connect BEFORE being asked to approve.
        await self._require_capability(tool, user_id)
        payload = args.model_dump(mode="json")
        if tool.effective_risk(args).needs_approval:
            auto = tool.name not in NEVER_AUTO_APPROVE and await policy_rules.matches(
                user_id, tool.name, payload
            )
            if not auto:
                preview = tool.render_preview(args, await tool_context(user_id))
                raise ApprovalRequired(tool.name, preview, payload)
        return await self._run(tool, user_id, args, actor="agent")

    async def execute_approved(self, approval_id: int) -> str:
        """Run a tool the user explicitly approved. Bypasses the approval check only."""
        approval = await approvals.get(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        tool = self.get(approval.tool)
        args = tool.args_model.model_validate(approval.arguments)
        await self._require_capability(tool, approval.user_id)
        return await self._run(tool, approval.user_id, args, actor="user_approved")

    async def _run(self, tool: MavisTool, user_id: int, args: BaseModel, actor: str) -> str:
        token = current_user_id.set(user_id)
        try:
            out = await tool.fn(user_id, args)
        finally:
            current_user_id.reset(token)
        text = out if isinstance(out, str) else json.dumps(out, default=str, ensure_ascii=False)
        text = truncate(text)
        if tool.effective_risk(args) is not RiskClass.READ:
            detail = {"args": args.model_dump(mode="json")}
            await audit.record(user_id, actor=actor, action=tool.name, detail=detail)
        return wrap_untrusted(text, tool.name) if tool.untrusted_output else text

    def _as_langchain(self, tool: MavisTool, user_id: int) -> BaseTool:
        async def _call(**kwargs: Any) -> str:
            args = tool.args_model.model_validate(kwargs)
            try:
                return await self.invoke(tool, user_id, args)
            except ApprovalRequired as req:
                approval_id = await approvals.create(
                    user_id=user_id,
                    task_id=current_task_id.get(),
                    tool=tool.name,
                    arguments=req.arguments,
                    preview=req.preview,
                    expires_at=utcnow() + timedelta(hours=get_settings().approval_ttl_hours),
                )
                log.info("tool.queued_for_approval", tool=tool.name, approval_id=approval_id)
                return (
                    f"QUEUED_FOR_APPROVAL #{approval_id}: {req.preview}\n"
                    "This has NOT been done yet. Tell the user it is ready and waiting for their OK."
                )

        return StructuredTool.from_function(
            coroutine=_call, name=tool.name, description=tool.description, args_schema=tool.args_model
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
