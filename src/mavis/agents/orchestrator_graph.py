"""Orchestrator graph.

    planner -> schedule -(Send per ready step)-> run_step -> schedule ... -> connect_gate -> critic
    connect_gate loops (one interrupt per missing capability; connected => re-run those steps)
    critic -(revise, at most MAX_REVISIONS)-> schedule | -> approval_gate
    approval_gate loops (one interrupt per pending approval) -> responder -> finish

State holds JSON-serialisable values only so any checkpointer can persist it.

Concurrency-1 rules (preflight F2): `route_ready` sends at most `task_step_parallelism` ready steps at
once (default 1, i.e. sequential), and every LLM call here runs at background priority with
fallback, so a chat reply always gets the next model slot.

Taint: a task created from a turn that saw untrusted tool output carries `tainted`, and every step
loop of that task runs tainted. A step that consumes a tainted step's output (as a dependency or
through critic feedback) also runs tainted. Tainted step text is wrapped as untrusted wherever a
later prompt includes it.
"""

from __future__ import annotations

import json
import mimetypes
import operator
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from mavis import bus
from mavis.agents import persona
from mavis.agents.spawn import spawn_agent
from mavis.agents.specialists import SPECIALISTS, get_specialist
from mavis.agents.specialists.base import current_deliverable, run_specialist
from mavis.channels.formatting import verbatim
from mavis.config import get_settings
from mavis.domain.decisions import ComposedMessage
from mavis.domain.errors import ActionFailed, BudgetExceeded, ConnectionRequired, LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.plans import CriticVerdict, Plan, PlanStep
from mavis.domain.tasks import ApprovalStatus, StepOutcome, TaskKind, TaskStatus
from mavis.llm import models as llm
from mavis.policy.risk import UNTRUSTED_NOTE, wrap_untrusted
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks, users
from mavis.tools.registry import current_task_id, get_registry, tool_context

log = structlog.get_logger()

MAX_REVISIONS = 2
MAX_STEPS = 8
_STEP_DIGEST_CHARS = 3000
_BG = {"priority": "background", "fallback": True}

PLANNER_PROMPT = """You are Mavis's planner. Break the user's goal into 1-6 steps for specialist agents.
Run independent steps in parallel by leaving depends_on empty; add depends_on only when a step needs another
step's output. Use short ids s1, s2, ...

Available agents:
{specialists}
- spawn: a generic worker for anything else. Set `tools` to a subset of: {spawn_tools}

Set deliverable to "message" unless the user asked for a file (pptx, pdf, docx, xlsx, chart).
Prefer the fewest steps that do the job well."""

CRITIC_PROMPT = """You are a strict reviewer. Given the goal and each step's output, decide whether the
combined results fully answer the goal. Accept unless a step is wrong, empty, failed for a fixable reason,
or clearly misses part of the goal. If revising, list only the step ids to redo and give concrete feedback."""

RESPONDER_RULES = """Summarise the outcome for the user in 1-3 short chat bubbles. Lead with the answer.
Mention anything that failed, was cancelled or is still waiting for their OK. Keep [n] citation markers and
put the source list in the last bubble. Do not mention internal step ids or agents."""

REVISE_PROMPT = """Revise the tool arguments exactly as the user asked. Change nothing else.
Return the complete revised arguments."""

REVISE_FAILED_TEXT = (
    "I couldn't apply that change. Want to say it another way, or should I send it as is?"
)

# Deterministic copy for APPROVAL-kind tasks (preflight F22): no LLM call to say "Done".
APPROVAL_TEXT = {
    "executed": "Done ✓",
    "rejected": "Okay, not doing that.",
    "expired": "That one expired, so I left it.",
    "failed": "Tried, but it failed: {reason}",
    "superseded": "That one was a duplicate of something already handled, so I closed it.",
    "past": ("That was for {when}, which has already passed, so I didn't do it. "
             "Ask me again with a new time if you still want it."),
}
NOTHING_TO_APPROVE_TEXT = "Nothing left to approve there."

# Set by run_step for the duration of one step; run_step_agent reads it when not told explicitly.
step_tainted: ContextVar[bool] = ContextVar("step_tainted", default=False)


def _err(exc: BaseException) -> str:
    """Type plus the first line of the message, short, for logs (the structlog processor masks tokens)."""
    first = (str(exc).splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first}"[:200]


def _clip_result(result: str, limit: int = 500) -> str:
    """Shorten a tool result without cutting off the closing tag of a wrapped (untrusted) result."""
    lines = result.split("\n")
    if len(lines) >= 2 and lines[0].startswith("<untrusted") and lines[-1] == "</untrusted>":
        return "\n".join([lines[0], "\n".join(lines[1:-1])[:limit], lines[-1]])
    return result[:limit]


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class OrchestratorState(TypedDict, total=False):
    task_id: int
    user_id: int
    kind: str
    goal: str
    context: str
    tainted: bool  # task-level taint (persisted on the task row)
    plan: dict | None
    todo: list[str]
    revision: int
    feedback: dict[str, str]
    feedback_tainted: bool  # the critic read tainted output when it wrote `feedback`
    results: Annotated[dict[str, dict], merge_dicts]
    action_results: Annotated[list[str], operator.add]
    approval_outcomes: Annotated[list[dict], operator.add]  # {"status", "preview", "detail"}
    revise_failures: Annotated[dict[str, int], merge_dicts]  # approval id (str) -> failed revisions
    artifacts: Annotated[list[str], operator.add]
    final_messages: list[str]
    connect_needed: Annotated[list[dict], operator.add]  # {"capability", "reason", "step_id", "revoked"}
    connect_done: Annotated[list[str], operator.add]  # capabilities already asked about


class StepInput(TypedDict):
    task_id: int
    user_id: int
    goal: str
    deliverable: str
    step: dict
    dep_results: dict[str, dict]
    feedback: str
    revision: int
    tainted: bool


def initial_state(task: Any) -> dict:
    return {
        "task_id": task.id, "user_id": task.user_id, "kind": task.kind, "goal": task.goal,
        "context": task.context or "", "tainted": bool(getattr(task, "tainted", False)),
        "plan": None, "todo": [], "revision": 0, "feedback": {}, "feedback_tainted": False,
        "results": {}, "action_results": [], "approval_outcomes": [], "revise_failures": {},
        "artifacts": [], "final_messages": [], "connect_needed": [], "connect_done": [],
    }


async def _cancelled(task_id: int) -> bool:
    task = await tasks.get(task_id)
    return task is None or task.status == TaskStatus.CANCELLED


# --- planning --------------------------------------------------------------------


def validate_plan(plan: Plan) -> None:
    if not plan.steps:
        raise ValueError("plan has no steps")
    if len(plan.steps) > MAX_STEPS:
        raise ValueError(f"plan has more than {MAX_STEPS} steps")
    ids = [s.id for s in plan.steps]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate step ids")
    for s in plan.steps:
        if s.agent != "spawn" and s.agent not in SPECIALISTS:
            raise ValueError(f"unknown agent {s.agent!r} in step {s.id}")
        for d in s.depends_on:
            if d not in ids:
                raise ValueError(f"step {s.id} depends on unknown step {d!r}")
    # Kahn's algorithm: every step must become ready eventually.
    remaining = {s.id: set(s.depends_on) for s in plan.steps}
    while remaining:
        ready = [sid for sid, deps in remaining.items() if not deps]
        if not ready:
            raise ValueError(f"dependency cycle among {sorted(remaining)}")
        for sid in ready:
            remaining.pop(sid)
        for deps in remaining.values():
            deps.difference_update(ready)


def _planner_system() -> str:
    specialists = "\n".join(f"- {s.name}: {s.description}" for s in SPECIALISTS.values())
    spawn_tools = ", ".join(get_registry().names_for("spawn")) or "(none)"
    return PLANNER_PROMPT.format(specialists=specialists, spawn_tools=spawn_tools)


async def make_plan(goal: str, context: str) -> Plan:
    system = f"{_planner_system()}\n\n{UNTRUSTED_NOTE}"
    user_msg = f"Goal:\n{goal}\n\nContext:\n{context or '(none)'}"
    for _ in range(2):
        plan = await llm.structured(Plan, system, user_msg, tier=llm.Tier.SMART, **_BG)
        try:
            validate_plan(plan)
            return plan
        except ValueError as exc:
            log.warning("planner.invalid_plan", error=str(exc))
            system = f"{system}\n\nYour previous plan was invalid: {exc}. Return a corrected plan."
    return Plan(goal=goal, steps=[PlanStep(id="s1", agent="research", instruction=goal)])


async def planner(state: OrchestratorState) -> dict:
    context = state.get("context", "")
    if context and state.get("tainted"):
        context = wrap_untrusted(context, "task_context")
    plan = await make_plan(state["goal"], context)
    # Status is owned by the task runner (QUEUED -> RUNNING claim); this never revives a finished task.
    await tasks.save_plan(state["task_id"], plan.model_dump())
    return {"plan": plan.model_dump(), "todo": [s.id for s in plan.steps], "revision": 0, "feedback": {}}


# --- scheduling and execution ----------------------------------------------------


async def schedule(state: OrchestratorState) -> dict:
    return {}


def _waiting_for_connection(state: OrchestratorState) -> set[str]:
    done = set(state.get("connect_done", []))
    return {c["step_id"] for c in state.get("connect_needed", []) if c["capability"] not in done}


async def route_ready(state: OrchestratorState) -> list[Send] | str:
    if await _cancelled(state["task_id"]):
        return END
    plan = Plan.model_validate(state["plan"])
    rev = state.get("revision", 0)
    results = state.get("results", {})
    todo = set(state.get("todo", []))
    waiting = _waiting_for_connection(state)
    feedback = state.get("feedback", {})

    def done_this_round(sid: str) -> bool:
        return results.get(sid, {}).get("round") == rev

    def dep_ready(dep: str) -> bool:
        # A dependency waiting on a connect prompt is not ready: its dependents run after the answer.
        return dep in results and dep not in waiting and (dep not in todo or done_this_round(dep))

    ready = [
        s for s in plan.steps
        if s.id in todo and not done_this_round(s.id) and all(dep_ready(d) for d in s.depends_on)
    ]
    if not ready:
        return "connect_gate"
    limit = max(1, get_settings().task_step_parallelism)
    sends: list[Send] = []
    for s in ready[:limit]:
        deps = {d: results[d] for d in s.depends_on}
        step_feedback = feedback.get(s.id, "")
        tainted = (
            bool(state.get("tainted"))
            or any(r.get("tainted") for r in deps.values())
            or bool(step_feedback and state.get("feedback_tainted"))
        )
        sends.append(Send("run_step", {
            "task_id": state["task_id"], "user_id": state["user_id"], "goal": state["goal"],
            "deliverable": plan.deliverable, "step": s.model_dump(), "dep_results": deps,
            "feedback": step_feedback, "revision": rev, "tainted": tainted,
        }))
    return sends


async def run_step_agent(
    step: PlanStep, user_id: int, context: str, *, tainted: bool | None = None
) -> StepOutcome:
    taint = step_tainted.get() if tainted is None else tainted
    if step.agent == "spawn":
        return await spawn_agent(user_id, role=f"worker {step.id}", goal=step.instruction,
                                 tools=step.tools, context=context, tainted=taint)
    return await run_specialist(get_specialist(step.agent), user_id, step.instruction, context, tainted=taint)


def _result_body(step_id: str, res: dict) -> str:
    if res.get("ok"):
        body = str(res.get("text") or "")[:_STEP_DIGEST_CHARS]
        return wrap_untrusted(body, f"step_{step_id}") if res.get("tainted") else body
    return f"FAILED: {str(res.get('error') or '')[:_STEP_DIGEST_CHARS]}"


def _step_context(inp: StepInput) -> str:
    parts = [f"Overall goal: {inp['goal']}"]
    for dep_id, res in inp["dep_results"].items():
        parts.append(f"Output of {dep_id}:\n{_result_body(dep_id, res)}")
    if inp["feedback"]:
        fb = inp["feedback"]
        if inp.get("tainted"):
            fb = wrap_untrusted(fb, "reviewer")
        parts.append(f"Reviewer feedback on your previous attempt (fix this): {fb}")
    return "\n\n".join(parts)


async def run_step(inp: StepInput) -> dict:
    step = PlanStep.model_validate(inp["step"])
    tainted = bool(inp.get("tainted", False))
    token = current_task_id.set(inp["task_id"])
    deliverable_token = current_deliverable.set(inp.get("deliverable", "message"))
    taint_token = step_tainted.set(tainted)
    try:
        outcome = await run_step_agent(step, inp["user_id"], _step_context(inp))
    except BudgetExceeded as exc:
        outcome = StepOutcome(ok=False, error=f"step ran out of budget: {exc}")
    except ConnectionRequired as exc:
        log.info("orchestrator.step_needs_connection", task_id=inp["task_id"], step=step.id,
                 capability=exc.capability.value)
        return {
            "results": {step.id: {"ok": False, "text": "", "artifacts": [], "round": inp["revision"],
                                  "agent": step.agent, "tainted": tainted,
                                  "error": f"waiting for {exc.capability.value} access"}},
            "connect_needed": [{"capability": exc.capability.value, "reason": exc.reason, "step_id": step.id,
                                "revoked": bool(exc.revoked)}],
        }
    except LLMError as exc:
        outcome = StepOutcome(ok=False, error=f"model error: {exc}")
    except Exception as exc:  # noqa: BLE001 - one broken step must not sink the whole task
        log.warning("orchestrator.step_crashed", task_id=inp["task_id"], step=step.id,
                    error_type=type(exc).__name__, error=_err(exc))
        outcome = StepOutcome(ok=False, error=f"step failed ({type(exc).__name__})")
    finally:
        current_task_id.reset(token)
        current_deliverable.reset(deliverable_token)
        step_tainted.reset(taint_token)
    log.info("orchestrator.step_done", task_id=inp["task_id"], step=step.id, ok=outcome.ok)
    return {
        "results": {step.id: {**outcome.model_dump(), "tainted": outcome.tainted or tainted,
                              "round": inp["revision"], "agent": step.agent}},
        "artifacts": outcome.artifacts,
    }


# --- review ------------------------------------------------------------------------


def _digest(state: OrchestratorState) -> str:
    plan = Plan.model_validate(state["plan"]) if state.get("plan") else None
    results = state.get("results", {})
    lines: list[str] = []
    for s in plan.steps if plan else []:
        r = results.get(s.id, {})
        status = "ok" if r.get("ok") else "FAILED"
        lines.append(f"[{s.id}: {s.agent}: {status}] {s.instruction}\n{_result_body(s.id, r)}")
    return "\n\n".join(lines)


def _any_tainted(state: OrchestratorState) -> bool:
    return bool(state.get("tainted")) or any(r.get("tainted") for r in state.get("results", {}).values())


async def critic(state: OrchestratorState) -> dict:
    rev = state.get("revision", 0)
    steps = state["plan"]["steps"]
    # Single-step plans skip review (preflight F26); so does a cancelled task.
    if rev >= MAX_REVISIONS or len(steps) < 2 or await _cancelled(state["task_id"]):
        return {"todo": []}
    verdict = await llm.structured(
        CriticVerdict, f"{CRITIC_PROMPT}\n\n{UNTRUSTED_NOTE}",
        f"Goal:\n{state['goal']}\n\nStep results:\n{_digest(state)}",
        tier=llm.Tier.SMART, **_BG,
    )
    step_ids = {s["id"] for s in steps}
    redo = [s for s in verdict.revise_steps if s in step_ids]
    if verdict.accept or not redo:
        return {"todo": []}
    return {"todo": redo, "revision": rev + 1, "feedback": {s: verdict.feedback for s in redo},
            "feedback_tainted": _any_tainted(state)}


def after_critic(state: OrchestratorState) -> str:
    return "schedule" if state.get("todo") else "approval_gate"


async def connect_gate(state: OrchestratorState) -> Command:
    """One interrupt per missing capability (same pattern as approval_gate: reads only before interrupt()).

    Interrupt payload: {"type": "connect", "capability", "reason", "step_ids", "revoked"}.
    Resume value: {"type": "connect", "capability", "connected": bool}.
    """
    done = set(state.get("connect_done", []))
    open_ = [c for c in state.get("connect_needed", []) if c["capability"] not in done]
    if not open_:
        return Command(goto="critic")
    cap = open_[0]["capability"]
    step_ids = sorted({c["step_id"] for c in open_ if c["capability"] == cap})
    answer = interrupt({"type": "connect", "capability": cap, "reason": open_[0]["reason"],
                        "step_ids": step_ids, "revoked": bool(open_[0].get("revoked", False))})
    update: dict[str, Any] = {"connect_done": [cap]}
    if isinstance(answer, dict) and answer.get("connected") is True:
        results = state.get("results", {})
        # round -1 marks the steps as not done this round, so route_ready dispatches them again.
        update["results"] = {sid: {**results.get(sid, {}), "round": -1} for sid in step_ids}
        update["todo"] = sorted(set(state.get("todo", [])) | set(step_ids))
        return Command(goto="schedule", update=update)
    # Describe the skipped work by instruction: the responder must not see internal step ids.
    instructions = {s["id"]: s["instruction"] for s in (state.get("plan") or {}).get("steps", [])}
    skipped = "; ".join(instructions.get(sid, "one part of the work") for sid in step_ids)
    update["action_results"] = [
        f"The user chose not to connect {cap} right now, so this was skipped: {skipped}"
    ]
    # Steps that depended on the skipped ones were held back; let them run without that input.
    return Command(goto="schedule", update=update)


# --- approvals -----------------------------------------------------------------------


async def revise_approval(approval: Any, instructions: str) -> None:
    tool = get_registry().get(approval.tool)
    revised = await llm.structured(
        tool.args_model, REVISE_PROMPT,
        f"Current arguments (JSON):\n{json.dumps(approval.arguments, ensure_ascii=False)}"
        f"\n\nUser's change request:\n{instructions}",
        tier=llm.Tier.FAST, **_BG,
    )
    ctx = await tool_context(approval.user_id)
    await approvals.update_args(
        approval.id, revised.model_dump(mode="json"), tool.render_preview(revised, ctx)
    )
    from mavis.policy import approvals as approval_flow  # lazy: policy.approvals imports the runner

    if (updated := await approvals.get(approval.id)) is not None:
        await approval_flow.sync_action_time_expiry(updated)  # the edit may have moved the action time


async def notify_revise_failed(user_id: int, approval_id: int, attempt: int) -> None:
    """Tell the user the edit did not apply (the gate then re-prompts with buttons)."""
    try:
        from mavis.policy import approvals as approval_flow  # lazy: policy.approvals imports the runner

        await approval_flow.say(user_id, REVISE_FAILED_TEXT,
                                dedupe_key=f"approval:{approval_id}:revise_failed:{attempt}")
    except Exception as exc:  # noqa: BLE001 - the re-prompt still follows
        log.warning("approval.revise_notice_failed", approval_id=approval_id, error=_err(exc))


async def _supersede_duplicates(decided: Any, *, executed: bool) -> None:
    try:
        from mavis.policy import approvals as approval_flow  # lazy: policy.approvals imports the runner

        await approval_flow.supersede_duplicates(decided, executed=executed)
    except Exception as exc:  # noqa: BLE001 - the decided action already stands
        log.warning("approval.supersede_failed", approval_id=decided.id, error=_err(exc))


async def _passed_action_time(approval: Any) -> str | None:
    from mavis.policy import approvals as approval_flow  # lazy: policy.approvals imports the runner

    return await approval_flow.passed_action_time(approval)


def _first_result_line(result: str) -> str:
    for line in result.splitlines():
        line = line.strip()
        if line and not line.startswith(("<untrusted", "</untrusted")):
            return verbatim(line[:200])  # tool output: shown as it came back
    return ""


async def approval_gate(state: OrchestratorState) -> Command:
    """One interrupt per pending approval. Everything before interrupt() is a read, so the
    node re-executing on resume (LangGraph semantics) has no side effects."""
    pending = await approvals.next_open(state["task_id"])
    if pending is None:
        return Command(goto=END if await _cancelled(state["task_id"]) else "responder")
    answer = interrupt({"type": "approval", "approval_id": pending.id, "tool": pending.tool,
                        "preview": pending.preview})
    if not isinstance(answer, dict) or answer.get("approval_id") != pending.id:
        return Command(goto="approval_gate")
    decision = answer.get("decision")
    passed = await _passed_action_time(pending) if decision in ("ok", "past") else None
    if passed is not None or decision == "past":
        # approving an action whose own time has gone is refused, whichever path approved it
        when = passed or "a time that has passed"
        if not await approvals.set_status(pending.id, ApprovalStatus.EXPIRED,
                                          result=f"action time passed ({when})",
                                          from_statuses={ApprovalStatus.RESOLVING}):
            return Command(goto="approval_gate")
        return Command(goto="approval_gate", update={
            "action_results": [f"Not done, its time ({when}) had already passed: {pending.preview}"],
            "approval_outcomes": [{"status": "past", "preview": pending.preview, "detail": when}],
        })
    if decision == "ok":
        # At most once (preflight F20): only the caller that moves RESOLVING -> EXECUTED runs the tool.
        # A crash after this point loses the confirmation, never duplicates the action.
        if not await approvals.claim(pending.id, {ApprovalStatus.RESOLVING}, ApprovalStatus.EXECUTED):
            return Command(goto="approval_gate")
        claimed = {ApprovalStatus.EXECUTED}
        # Execution marker for the restart sweep: claimed-but-never-run vs may-have-run.
        await approvals.mark_started(pending.id)
        try:
            result = await get_registry().execute_approved(pending.id)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the task
            if isinstance(exc, ActionFailed):
                reason = exc.reason
            else:
                reason = (str(exc).splitlines() or [""])[0] or type(exc).__name__
            reason = reason[:150]
            await approvals.set_status(pending.id, ApprovalStatus.FAILED, result=str(exc)[:500],
                                       from_statuses=claimed)
            log.warning("approval.execute_failed", approval_id=pending.id, tool=pending.tool, error=_err(exc))
            return Command(goto="approval_gate", update={
                "action_results": [
                    f"Failed: {pending.preview}\nReason: {wrap_untrusted(reason, pending.tool)}"
                ],
                "approval_outcomes": [{"status": "failed", "preview": pending.preview, "detail": reason}],
            })
        await approvals.set_status(pending.id, ApprovalStatus.EXECUTED, result=result, from_statuses=claimed)
        await _supersede_duplicates(pending, executed=True)
        return Command(goto="approval_gate", update={
            "action_results": [f"Done: {pending.preview}\nResult: {_clip_result(result)}"],
            "approval_outcomes": [{"status": "executed", "preview": pending.preview,
                                   "detail": _first_result_line(result)}],
        })
    if decision == "edit":
        try:
            await revise_approval(pending, str(answer.get("instructions", "")))
        except Exception as exc:  # noqa: BLE001 - an edit must never strand the approval (F21)
            log.warning("approval.revise_failed", approval_id=pending.id, error=_err(exc))
            await approvals.update_args(pending.id, pending.arguments, pending.preview)
            attempt = state.get("revise_failures", {}).get(str(pending.id), 0) + 1
            await notify_revise_failed(pending.user_id, pending.id, attempt)
            return Command(goto="approval_gate", update={"revise_failures": {str(pending.id): attempt}})
        return Command(goto="approval_gate")
    if decision == "superseded":
        # The same action was already decided on another card: close this one, run nothing.
        if not await approvals.set_status(pending.id, ApprovalStatus.REJECTED, result="superseded",
                                          from_statuses={ApprovalStatus.RESOLVING}):
            return Command(goto="approval_gate")
        return Command(goto="approval_gate", update={
            "action_results": [f"Skipped (already handled on another card): {pending.preview}"],
            "approval_outcomes": [{"status": "superseded", "preview": pending.preview, "detail": ""}],
        })
    status = ApprovalStatus.EXPIRED if decision == "expired" else ApprovalStatus.REJECTED
    if not await approvals.set_status(pending.id, status):
        return Command(goto="approval_gate")  # already resolved elsewhere; don't report a stale outcome
    if status is ApprovalStatus.REJECTED:
        await _supersede_duplicates(pending, executed=False)
    return Command(goto="approval_gate", update={
        "action_results": [f"{status.value.title()}: {pending.preview}"],
        "approval_outcomes": [{"status": status.value, "preview": pending.preview, "detail": ""}],
    })


# --- response --------------------------------------------------------------------------


def _approval_messages(outcomes: list[dict]) -> list[str]:
    texts: list[str] = []
    for o in outcomes:
        status, detail = o.get("status"), str(o.get("detail") or "")
        if status == "executed":
            texts.append(f"{APPROVAL_TEXT['executed']}\n{detail}" if detail else APPROVAL_TEXT["executed"])
        elif status == "failed":
            texts.append(APPROVAL_TEXT["failed"].format(reason=verbatim(detail) or "unknown error"))
        elif status == "past":
            texts.append(APPROVAL_TEXT["past"].format(when=detail or "a time that has passed"))
        elif status in APPROVAL_TEXT:
            texts.append(APPROVAL_TEXT[status])
    if not texts:
        return [NOTHING_TO_APPROVE_TEXT]
    if len(texts) > 3:
        texts = [*texts[:2], "\n".join(texts[2:])]
    return texts


async def responder(state: OrchestratorState) -> dict:
    if state.get("kind") == TaskKind.APPROVAL:
        return {"final_messages": _approval_messages(state.get("approval_outcomes", []))}
    user = await users.get(state["user_id"])
    actions = "\n".join(state.get("action_results", [])) or "(none)"
    system = f"{persona.system_prompt(user, utcnow(), '')}\n\n{RESPONDER_RULES}\n\n{UNTRUSTED_NOTE}"
    msg = await llm.structured(
        ComposedMessage, system,
        f"The user asked: {state['goal']}\n\nWork results:\n{_digest(state) or '(no research steps)'}"
        f"\n\nActions:\n{actions}",
        tier=llm.Tier.SMART, **_BG,
    )
    texts = [m.strip() for m in msg.messages if m.strip()][:3] or ["Done."]
    return {"final_messages": texts}


async def finish(state: OrchestratorState) -> dict:
    task_id, user_id = state["task_id"], state["user_id"]
    messages = state.get("final_messages", [])
    artifacts = list(dict.fromkeys(state.get("artifacts", [])))
    recorded = {a.path for a in await tasks.artifacts_for(task_id)}  # specialists may record their own
    for path in (p for p in artifacts if p not in recorded):
        await tasks.add_artifact(
            task_id, user_id, kind=Path(path).suffix.lstrip(".") or "file", path=path,
            mime=mimetypes.guess_type(path)[0] or "application/octet-stream",
        )
    # Terminal transition by claim: a concurrent cancel wins and nothing is delivered.
    active = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL)
    fields: dict[str, Any] = {"result_text": "\n\n".join(messages)}
    if _any_tainted(state):
        # Taint picked up mid-run (a step read an email) lives only in the graph state: store it on the
        # row too, so delivery, redelivery and the next chat turn treat the result as untrusted.
        fields["tainted"] = True
    if not await tasks.claim(task_id, active, TaskStatus.DONE, **fields):
        log.info("orchestrator.finish_skipped", task_id=task_id)
        return {}
    task = await tasks.get(task_id)
    await bus.get_bus().publish(Event(
        id=f"task:{task_id}:completed", user_id=user_id, type=EventType.TASK_COMPLETED,
        occurred_at=utcnow(), source="agent", trust=Trust.SYSTEM,
        payload={
            "task_id": task_id, "messages": messages, "artifacts": artifacts,
            "origin": task.origin, "notify_on_complete": task.notify_on_complete,
            "tainted": bool(task.tainted),
        },
    ))
    return {}


def entry_route(state: OrchestratorState) -> str:
    return "approval_gate" if state.get("kind") == TaskKind.APPROVAL else "planner"


def build_orchestrator() -> StateGraph:
    g = StateGraph(OrchestratorState)
    g.add_node("planner", planner)
    g.add_node("schedule", schedule)
    g.add_node("run_step", run_step)
    g.add_node("critic", critic)
    g.add_node("connect_gate", connect_gate, destinations=("connect_gate", "schedule", "critic"))
    g.add_node("approval_gate", approval_gate, destinations=("approval_gate", "responder", END))
    g.add_node("responder", responder)
    g.add_node("finish", finish)
    g.add_conditional_edges(START, entry_route, ["planner", "approval_gate"])
    g.add_edge("planner", "schedule")
    g.add_conditional_edges("schedule", route_ready, ["run_step", "connect_gate", END])
    g.add_edge("run_step", "schedule")
    g.add_conditional_edges("critic", after_critic, ["schedule", "approval_gate"])
    g.add_edge("responder", "finish")
    g.add_edge("finish", END)
    return g
