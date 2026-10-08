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
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from mavis.config import get_settings
from mavis.domain.errors import ActionFailed, ApprovalRequired, ConnectionRequired, NeedsUserDetail
from mavis.domain.localtime import has_datetimes, localize_args
from mavis.domain.policy import Capability, RiskClass
from mavis.domain.results import ToolOutput
from mavis.domain.tasks import ApprovalStatus
from mavis.domain.terms import from_user_not_sources, grounded_in, same_request, terms
from mavis.policy.risk import truncate, wrap_untrusted
from mavis.store.db import utcnow
from mavis.store.repo import approvals, audit, policy_rules, tasks

log = structlog.get_logger()

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_terms = terms  # select()'s overlap vocabulary (domain.terms)


NEVER_AUTO_APPROVE = frozenset({"add_policy_rule", "forget"})
_PREVIEW_IN_RESULT_CHARS = 500
ALREADY_WAITING_RESULT = (
    "ALREADY_AWAITING_APPROVAL #{id}: this same action is already waiting for the user's OK on an "
    "earlier card. Nothing new was queued and it has NOT been done yet. Tell the user it's waiting on "
    "that card; do not queue it again."
)

# The card (preview + buttons, rendered by code) is the only prompt for an approval: the model's reply
# must not repeat it or ask for a tap (track 1 T1.1). A turn whose every tool result is one of these
# sends no prose bubble at all (agents.conversation).
CARD_NOTE = ("It has NOT been done yet. The user gets a card with this action and buttons to approve, edit "
             "or cancel right after your reply: that card is the prompt. Do not describe it, ask them to "
             "approve or tap anything, or say it was done.")
QUEUED_PREFIX = "QUEUED_FOR_APPROVAL"
UPDATED_PREFIX = "UPDATED_WAITING_APPROVAL"
CARD_RESULT_PREFIXES = (QUEUED_PREFIX, UPDATED_PREFIX)
UPDATED_WAITING_RESULT = (
    UPDATED_PREFIX + " #{id}: {shown}\nThe card already waiting for this was updated to this version and "
    "is shown again. " + CARD_NOTE + " Do not queue it again."
)

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
    # Taint for self-only actions (track 1, T1.1): their effects stay with the user, so they are judged by
    # what can steer THIS turn (its own untrusted reads and the reply just before the user's message),
    # not by the whole replayed window that `tainted` covers. None: same as `tainted` (task loops).
    self_tainted: bool | None = None
    user_words: str = ""  # the user's own message this turn: the provenance of a self-only request
    # Third-party text that reached the prompt (tainted replies in the window, this run's untrusted reads):
    # an argument is the user's when it repeats nothing from it. None: unknown (hook context), then only an
    # argument made of the user's own words counts.
    untrusted_sources: list[str] | None = None
    untrusted_seen: bool = False  # an untrusted_output tool returned during the current step
    untrusted_reads: int = 0  # every time third-party text reached the model in this run (never reset)
    queued_approvals: list[int] = field(default_factory=list)
    spawned: int = 0  # workers started in the current outermost model step (reset by react_loop)
    memo: dict[str, Any] = field(default_factory=dict)  # per-run cache for `prepare` lookups (file metadata)

    def saw_untrusted(self, text: str = "") -> None:
        if self.untrusted_sources is not None:
            if text:
                self.untrusted_sources.append(text)
            else:
                self.untrusted_sources = None  # third-party text of unknown wording: back to strict
        self.untrusted_seen = True
        self.untrusted_reads += 1

    def end_step(self) -> None:
        self.tainted = self.tainted or self.untrusted_seen
        if self.self_tainted is not None:
            self.self_tainted = self.self_tainted or self.untrusted_seen
        self.untrusted_seen = False

    def self_taint(self) -> bool:
        return self.tainted if self.self_tainted is None else self.self_tainted


# Capabilities a step must do without (the user never asked for them and they are not connected): their
# tools are not offered to the step's loop. Set by the orchestrator around a re-run of one step.
excluded_capabilities: ContextVar[frozenset[Capability]] = ContextVar("excluded_capabilities",
                                                                      default=frozenset())

current_run: ContextVar[ToolRun | None] = ContextVar("current_run", default=None)


def _run_tainted() -> bool:
    run = current_run.get()
    return run is not None and run.tainted


SELF_ONLY = frozenset({RiskClass.READ, RiskClass.WRITE_SELF})


def self_only_tainted() -> bool:
    """The taint a self-only action of this run is judged by (ToolRun.self_tainted)."""
    run = current_run.get()
    return run is not None and run.self_taint()


def _user_worded(tool: MavisTool, args: BaseModel, run: ToolRun, *, relaxed: bool = False) -> bool:
    """Every provenance argument the tool declares is the user's own words this turn (domain.terms).

    Strict (default): every content term and identifier is one the user wrote. `relaxed` also accepts the
    model's own natural wording that copies nothing from untrusted text (domain.terms.from_user_not_sources).
    Only the CARD for a self-only action may use the relaxed rule; what a call stores is judged strictly, so
    a paraphrase of planted text can skip a card but is never saved as the user's own."""
    values = [getattr(args, name, "") for name in tool.provenance]
    texts = [v for v in values if isinstance(v, str) and v.strip()]
    if not texts or len(texts) != len(values):
        return False
    sources = run.untrusted_sources
    return all(grounded_in(t, run.user_words) or (
        relaxed and sources is not None and from_user_not_sources(t, run.user_words, sources))
        for t in texts)


def _gate_tainted(tool: MavisTool, args: BaseModel, risk: RiskClass) -> bool:
    """Does this call need its taint policy's CARD (TaintPolicy.APPROVE)?

    Outward, spending and destructive calls: the whole run's taint (the replayed window included).
    Self-only calls (READ / WRITE_SELF, their effects stay with the user): only what can steer this turn
    (ToolRun.self_tainted), and not even that when the call is worded entirely in the user's own words
    this turn. Skipping the card never changes the trust of what the call stores: see _persist_untrusted."""
    if risk not in SELF_ONLY:
        return _run_tainted()
    run = current_run.get()
    if run is None or not run.self_taint():
        return False
    # A durable trusted store (DOWNGRADE tools: remember) never gets the relaxed waiver.
    if _user_worded(tool, args, run, relaxed=tool.on_taint is not TaintPolicy.DOWNGRADE):
        log.info("tool.taint_waived_user_words", tool=tool.name)
        return False
    return True


def _persist_untrusted(tool: MavisTool, args: BaseModel) -> bool:
    """Is what this call stores (a fact, a loop, a reminder, a task) third-party shaped?

    Yes whenever third-party text was anywhere in the run's prompt (the whole window, ToolRun.tainted),
    unless the call is worded entirely in the user's own words this turn: then its content is theirs."""
    run = current_run.get()
    if run is None or not run.tainted:
        return False
    return not _user_worded(tool, args, run)


# Set by the registry around one tool call (track 1 T1.1 fix round 1): the call persists content derived
# from third-party text, so the tool stores it untrusted (a loop, a reminder that fires on the untrusted
# path). Approved calls never set it: the user saw the card and said yes.
current_call_untrusted: ContextVar[bool] = ContextVar("current_call_untrusted", default=False)


def call_untrusted() -> bool:
    return current_call_untrusted.get()


async def _queue_tainted(task_id: int | None) -> bool:
    """Is a request being queued for approval third-party shaped? The run saw untrusted output (this
    step or earlier), or it runs for a tainted task."""
    run = current_run.get()
    if run is not None and (run.tainted or run.untrusted_seen):
        return True
    if task_id is None:
        return False
    task = await tasks.get(task_id)
    return bool(task is not None and task.tainted)


async def _waiting_same_request(user_id: int, tool: MavisTool, arguments: dict, tainted: bool):
    """A card already waiting for the same request in other words: tools that declare a provenance
    argument (the text that says what the request is) compare it by content terms, so a re-asked request
    ("yes go ahead" answered by a model that words the goal again) never makes a second card."""
    if not tool.provenance:
        return None
    mine = " ".join(str(arguments.get(name) or "") for name in tool.provenance)
    rest = approvals.equivalence_key({k: v for k, v in arguments.items() if k not in tool.provenance})
    for row in await approvals.waiting_of_tool(user_id, tool.name, tainted=tainted):
        theirs = " ".join(str((row.arguments or {}).get(name) or "") for name in tool.provenance)
        other = {k: v for k, v in (row.arguments or {}).items() if k not in tool.provenance}
        # the same words for another time or kind (a reminder at 5 and at 6) are another request
        if approvals.equivalence_key(other) == rest and same_request(mine, theirs):
            return row
    return None


ToolFn = Callable[[int, Any], Awaitable[str | dict | list | ToolOutput]]
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


async def _localized(args: BaseModel, user_id: int) -> BaseModel:
    """Every datetime argument is the user's wall-clock time: attach the zone in code (domain.localtime)."""
    if not has_datetimes(type(args)):
        return args
    return localize_args(args, (await tool_context(user_id)).timezone)


def contextual(fn: Callable[[ToolContext, Any], Awaitable[str | dict | list | ToolOutput]]) -> ToolFn:
    """Adapt an `async fn(ctx, args)` to the registry's `async fn(user_id, args)` signature."""

    async def wrapped(user_id: int, args: Any) -> str | dict | list | ToolOutput:
        return await fn(await tool_context(user_id), args)

    wrapped.__name__ = getattr(fn, "__name__", "contextual")
    return wrapped


def default_label(name: str) -> str:
    """The humanised tool name: the card's "Last:" line when a tool has no progress_label."""
    return name.replace("_", " ").strip()


def host_of(url: str) -> str:
    """Lower-cased host without "www.", or "" when the text is not a URL with a host."""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


async def _note_progress(tool: MavisTool, args: BaseModel, raw: Any) -> None:
    """Tell the task's card what just ran, with a code-made label. Never fails the tool call."""
    task_id = current_task_id.get()
    if task_id is None or not get_settings().progress_card_enabled:
        return
    try:
        label = tool.progress_label(args, raw) if tool.progress_label else default_label(tool.name)
        from mavis.channels.progress_card import get_cards  # lazy: channels import the bus

        await get_cards().tool_called(task_id, label)
    except Exception as exc:  # noqa: BLE001 - cosmetic
        log.debug("tool.progress_label_failed", tool=tool.name, error=type(exc).__name__)


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
    # What makes two calls the SAME action (approval dedupe and supersede): the arguments that determine
    # what the action does. Empty, or any of them empty in a call, means the whole canonical argument
    # set is the identity.
    identity: tuple[str, ...] = ()
    # A coarser "same target" key (an invite: start + attendees; an email: to + subject). A new request
    # with the same target but other content corrects the waiting card instead of queuing a second one.
    # Empty, or any of them empty in a call, means no target matching.
    target: tuple[str, ...] = ()
    # The argument holding when the action takes effect (an event start). An approval whose action
    # time has passed expires and can no longer be approved.
    action_time: str | None = None
    # Free-text arguments that state what a self-only call is for (a task's goal, a reminder's reason).
    # When all of them come from the user's own words this turn, the call is the user's request and its
    # taint policy does not apply (see _gate_tainted). Empty: provenance never waives the policy.
    provenance: tuple[str, ...] = ()
    # Code-made "Last:" line for the progress card (Phase 12). Gets the arguments and the raw return
    # value; must never include page text, file contents or model prose (hosts, counts and exit codes only).
    progress_label: Callable[[BaseModel, Any], str] | None = None

    def effective_risk(self, args: BaseModel) -> RiskClass:
        return self.risk_fn(args) if self.risk_fn is not None else self.risk

    def render_preview(self, args: BaseModel, ctx: ToolContext | None = None) -> str:
        if self.preview is not None:
            if self.preview_needs_ctx:
                return self.preview(args, ctx or ToolContext(user_id=0))
            return self.preview(args)
        return f"{self.name} {args.model_dump_json()}"


@dataclass(frozen=True)
class Executed:
    """An approved action that ran (hotfix4 H3). `text` is the full result for the model and the
    approval row; `user_text` is the only part a user-facing receipt may show (empty when the tool
    returned a plain, model-only string)."""

    text: str
    user_text: str = ""


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

    def find(self, name: str) -> MavisTool | None:
        return self._tools.get(name)

    def names_for(self, agent: str) -> list[str]:
        return [t.name for t in self._tools.values() if agent in t.agents]

    @staticmethod
    def _excluded(tool: MavisTool) -> bool:
        return tool.requires is not None and tool.requires in excluded_capabilities.get()

    def for_agent(self, agent: str, user_id: int, names: Iterable[str] | None = None) -> list[BaseTool]:
        wanted = set(names) if names is not None else None
        return [
            self._as_langchain(t, user_id)
            for t in self._tools.values()
            if agent in t.agents and self.available(t) and (wanted is None or t.name in wanted)
            and not self._excluded(t)
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
                      if agent in t.agents and self.available(t) and t.name not in banned
                      and not self._excluded(t)]
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
        args = await _localized(args, user_id)  # wall-clock times get their zone here, for every tool
        payload = args.model_dump(mode="json")
        prepared = await self._prepared(tool, user_id, args)
        if prepared.refusal is not None:
            return prepared.refusal  # refused before approval: nothing runs and nothing is queued
        risk = prepared.risk or tool.effective_risk(args)
        note = f"\n{prepared.note}" if prepared.note else ""
        tainted = _run_tainted()  # outward: standing rules never auto-approve after third-party text
        if _gate_tainted(tool, args, risk) and tool.on_taint is TaintPolicy.APPROVE:
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
        untrusted = risk in SELF_ONLY and _persist_untrusted(tool, args)
        if tool.on_taint is TaintPolicy.DOWNGRADE and tool.tainted_fn is not None and (
                untrusted or _gate_tainted(tool, args, risk)):
            log.info("tool.taint_downgraded", tool=tool.name)
            return await self._run(tool, user_id, args, actor="agent", fn=tool.tainted_fn)
        token = current_call_untrusted.set(untrusted)
        try:
            return await self._run(tool, user_id, args, actor="agent")
        finally:
            current_call_untrusted.reset(token)

    async def execute_approved(self, approval_id: int) -> Executed:
        """Run a tool the user explicitly approved. Bypasses the approval check only."""
        approval = await approvals.get(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        tool = self.get(approval.tool)
        try:
            args = tool.args_model.model_validate(approval.arguments)
        except ValidationError as exc:
            # A request saved under older rules (or edited into an invalid shape): fail it with a
            # sentence the user can act on, never a validation dump.
            causes = [e.get("ctx", {}).get("error") for e in exc.errors()]
            user_text = next((c.user_text for c in causes if isinstance(c, NeedsUserDetail)),
                             "Some details of this request are missing now, so I didn't do it. "
                             "Ask me again?")
            log.warning("tool.approved_args_invalid", tool=tool.name, approval_id=approval_id)
            raise ActionFailed(f"Saved arguments are not valid: {exc.errors()[0].get('msg', '')}",
                               reason=user_text) from None
        await self._require_capability(tool, approval.user_id)
        args = await _localized(args, approval.user_id)  # idempotent on stored, already zoned times
        # The action runs on behalf of the task that held the approval (tools may read its taint).
        task_token = current_task_id.set(approval.task_id)
        try:
            # raise_errors: a failed approved action must surface as an exception, never as a result
            # string, so the caller records FAILED instead of EXECUTED.
            text, user_text = await self._execute(tool, approval.user_id, args, actor="user_approved",
                                                  raise_errors=True)
        finally:
            current_task_id.reset(task_token)
        return Executed(text=text, user_text=user_text)

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
        """Run for the model: the text it reads (user_text and model_note together)."""
        return (await self._execute(tool, user_id, args, actor, fn, raise_errors=raise_errors))[0]

    async def _execute(
        self,
        tool: MavisTool,
        user_id: int,
        args: BaseModel,
        actor: str,
        fn: ToolFn | None = None,
        *,
        raise_errors: bool = False,
    ) -> tuple[str, str]:
        """(text for the model, plain text a user-facing receipt may show)."""
        token = current_user_id.set(user_id)
        detail: dict[str, Any] = {"args": args.model_dump(mode="json")}
        if fn is not None:
            detail["tainted"] = True
        audited = tool.effective_risk(args) is not RiskClass.READ
        run = current_run.get()
        failed = False
        try:
            out = await (fn or tool.fn)(user_id, args)
            await _note_progress(tool, args, out)
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
                    run.saw_untrusted(str(exc))
                return wrap_untrusted(truncate(f"Tool error: {exc}"), tool.name), ""
            raise
        finally:
            current_user_id.reset(token)
        user_text = ""
        if isinstance(out, ToolOutput):
            user_text, out = out.user_text, out.for_model()
        text = out if isinstance(out, str) else json.dumps(out, default=str, ensure_ascii=False)
        text = truncate(text)
        if audited and not failed:
            await audit.record(user_id, actor=actor, action=tool.name, detail={**detail, "outcome": "ok"})
        if not tool.untrusted_output:
            return text, user_text
        if run is not None:
            run.saw_untrusted(text)
        return wrap_untrusted(text, tool.name), user_text

    def _as_langchain(self, tool: MavisTool, user_id: int) -> BaseTool:
        async def _call(**kwargs: Any) -> str:
            args = tool.args_model.model_validate(kwargs)
            try:
                return await self.invoke(tool, user_id, args)
            except ApprovalRequired as req:
                task_id = current_task_id.get()
                tainted = await _queue_tainted(task_id)
                async with _queue_lock():
                    existing = await approvals.find_open(user_id, task_id, tool.name, req.arguments)
                    twin = None if existing is not None else next(iter(
                        await approvals.waiting_equivalents(user_id, tool.name, req.arguments,
                                                            identity=tool.identity, tainted=tainted)),
                        None)
                    if twin is None and existing is None:
                        twin = await _waiting_same_request(user_id, tool, req.arguments, tainted)
                    if twin is not None:
                        # The same action already waits on the user (another task, or an earlier
                        # turn): one card per action, never a second one to approve twice.
                        log.info("tool.approval_already_waiting", tool=tool.name, approval_id=twin.id)
                        return ALREADY_WAITING_RESULT.format(id=twin.id)
                    corrected = None if existing is not None else next(iter(
                        await approvals.waiting_same_target(user_id, tool.name, req.arguments,
                                                            target=tool.target, tainted=tainted,
                                                            statuses=[ApprovalStatus.PENDING])), None)
                    if corrected is not None:
                        # A corrected version of a card still waiting on a tap: update that card rather
                        # than report a "duplicate" whose card shows the old content, or queue a second.
                        if await approvals.update_args(corrected.id, req.arguments, req.preview):
                            await audit.record(user_id, actor="agent", action="approval.updated",
                                               detail={"approval_id": corrected.id, "tool": tool.name})
                        else:  # lost a race with a tap: the card was not changed, so never say it was
                            log.info("tool.approval_update_lost", tool=tool.name, approval_id=corrected.id)
                            return ALREADY_WAITING_RESULT.format(id=corrected.id)
                    if existing is not None:
                        approval_id = existing.id
                    elif corrected is not None:
                        approval_id = corrected.id
                    else:
                        approval_id = await approvals.create(
                            user_id=user_id,
                            task_id=task_id,
                            tool=tool.name,
                            arguments=req.arguments,
                            preview=req.preview,
                            expires_at=utcnow() + timedelta(hours=get_settings().approval_ttl_hours),
                            tainted=tainted,
                        )
                if corrected is not None:
                    from mavis.policy import approvals as approval_flow  # lazy: policy imports registry

                    await approval_flow.send_approval_prompt(user_id, {"approval_id": corrected.id})
                    log.info("tool.approval_updated", tool=tool.name, approval_id=corrected.id)
                    shown = wrap_untrusted(truncate(req.preview, _PREVIEW_IN_RESULT_CHARS),
                                           "approval_preview")
                    return UPDATED_WAITING_RESULT.format(id=corrected.id, shown=shown)
                log.info("tool.queued_for_approval", tool=tool.name, approval_id=approval_id)
                run = current_run.get()
                if run is not None and approval_id not in run.queued_approvals:
                    run.queued_approvals.append(approval_id)
                shown = wrap_untrusted(truncate(req.preview, _PREVIEW_IN_RESULT_CHARS), "approval_preview")
                return f"{QUEUED_PREFIX} #{approval_id}: {shown}\n{CARD_NOTE}"

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
