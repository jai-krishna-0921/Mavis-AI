"""Approval UX: prompts with buttons, button/text decisions, reminders and expiry.

Decisions never execute anything here. They move the approval to RESOLVING
(atomically, so double taps are harmless) and enqueue RESUME_TASK; the
orchestrator's approval_gate performs the action.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Literal
from uuid import uuid4

import structlog
from pydantic import BaseModel

from mavis import bus
from mavis.channels.formatting import sanitize_line
from mavis.config import get_settings
from mavis.domain.events import Event, Job, JobKind
from mavis.domain.messages import TAINT_SUFFIX, Button, Outbound, Role
from mavis.domain.tasks import ApprovalStatus, TaskStatus
from mavis.llm import models as llm
from mavis.store.db import Session, utcnow
from mavis.store.repo import approvals, audit, messages, outbox, tasks
from mavis.timers import service as timers_service

log = structlog.get_logger()


def approval_buttons(approval_id: int) -> list[list[Button]]:
    return [[
        Button(label="✅ Send", data=f"ap:{approval_id}:ok"),
        Button(label="✏️ Edit", data=f"ap:{approval_id}:edit"),
        Button(label="❌ Cancel", data=f"ap:{approval_id}:no"),
    ]]


async def say(user_id: int, text: str, buttons: list[list[Button]] | None = None,
              dedupe_key: str | None = None, tainted: bool = False) -> None:
    """`tainted`: the text carries third-party content (a tainted task's approval preview). It is logged
    with the taint marker so the next turn runs tainted and learns it as untrusted."""
    async with Session() as s:
        # A deduped send is not a new message: it must not be logged to history a second time.
        is_new = not (dedupe_key and await outbox.exists_with_key(s, dedupe_key))
        await outbox.enqueue(
            s, Outbound(user_id=user_id, text=text, buttons=buttons or [], dedupe_key=dedupe_key)
        )
        await s.commit()
    if is_new:
        event_id = f"say:{dedupe_key or uuid4().hex}{TAINT_SUFFIX}" if tainted else None
        await messages.log(user_id, Role.ASSISTANT, text, event_id=event_id)


async def _task_tainted(approval) -> bool:
    if approval.task_id is None:
        return False
    task = await tasks.get(approval.task_id)
    return bool(task is not None and task.tainted)


async def send_approval_prompt(user_id: int, payload: dict) -> None:
    approval = await approvals.get(int(payload["approval_id"]))
    if approval is None or approval.status != ApprovalStatus.PENDING:
        return
    text = f"Ready when you are. Want me to go ahead?\n\n{approval.preview}"
    await say(user_id, text, approval_buttons(approval.id),
              dedupe_key=f"approval:{approval.id}:{int(utcnow().timestamp() * 1000)}",
              tainted=await _task_tainted(approval))
    if await approvals.mark_prompted(approval.id):
        ttl = timedelta(hours=get_settings().approval_ttl_hours)
        now = utcnow()
        wakeups = timers_service.WakeupService()
        await wakeups.wake_me(user_id, now + ttl - timedelta(hours=2), f"approval:{approval.id}",
                              kind="system_approval_remind", scale=False,
                              dedupe_key=f"approval:{approval.id}:remind")
        await wakeups.wake_me(user_id, now + ttl, f"approval:{approval.id}", kind="system_approval_expire",
                              scale=False, dedupe_key=f"approval:{approval.id}:expire")


# --- decisions ------------------------------------------------------------------------

_BUTTON = re.compile(r"^ap:(\d+):(ok|edit|no)$")
_REASON = re.compile(r"^approval:(\d+)$")
_OPENABLE = {ApprovalStatus.PENDING, ApprovalStatus.AWAITING_EDIT}
_DONE = {ApprovalStatus.EXECUTED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.FAILED}

INTERPRET_PROMPT = """The user was shown a pending action and asked to approve it. Classify their reply:
- approve: they want it done as is ("yes", "send it", "go ahead")
- cancel: they don't want it ("no", "don't", "cancel")
- edit: they want changes; put the requested change in `instructions`
- unrelated: the reply is about something else entirely"""

EDIT_QUESTION = "Sure, what should I change?"

# Plain one-word style answers need no model call to classify.
_QUICK = {
    **dict.fromkeys(("ok", "okay", "k", "yes", "yep", "yeah", "y", "sure", "send", "send it", "go ahead",
                     "do it", "approve", "approved", "go", "ship it", "sounds good", "looks good"),
                    "approve"),
    **dict.fromkeys(("no", "nope", "n", "no thanks", "cancel", "cancel it", "dont", "don't", "don't send",
                     "dont send", "stop", "never mind", "nevermind", "forget it"), "cancel"),
}
_QUICK_STRIP = re.compile(r"[\s.!,👍✅]+")

# A decision in flight longer than this lost its resume job. It must exceed the bus claim idle time
# (15 min), so the startup sweep never fails a row whose RESUME_TASK job is still queued or redelivered.
STALE_AFTER = timedelta(minutes=20)
_AFTER_STOP_WINDOW = timedelta(days=1)


class ApprovalReplyInterpretation(BaseModel):
    decision: Literal["approve", "cancel", "edit", "unrelated"]
    instructions: str = ""


def approval_id_from_reason(reason: str) -> int | None:
    m = _REASON.match(reason or "")
    return int(m.group(1)) if m else None


async def _resume(approval, decision: str, instructions: str = "") -> None:
    """Hand a recorded decision (approval already RESOLVING) to the task, or close a taskless row."""
    if approval.task_id is None:
        final = {"no": ApprovalStatus.REJECTED, "expired": ApprovalStatus.EXPIRED}.get(
            decision, ApprovalStatus.FAILED)
        await approvals.set_status(approval.id, final, "no task to resume",
                                   from_statuses={ApprovalStatus.RESOLVING})
        if final == ApprovalStatus.FAILED:
            await say(approval.user_id, "I lost track of what that was for, so I didn't do it. "
                      "Ask me again if you still want it.", dedupe_key=f"approval:{approval.id}:no_task")
        log.warning("approval.no_task", approval_id=approval.id)
        return
    await bus.get_bus().enqueue(Job(
        id=f"resume:{approval.id}:{decision}:{uuid4().hex[:6]}", user_id=approval.user_id,
        kind=JobKind.RESUME_TASK,
        payload={"task_id": approval.task_id, "approval_id": approval.id, "decision": decision,
                 "instructions": instructions},
    ))


async def handle_approval_button(event: Event) -> None:
    m = _BUTTON.match(str(event.payload.get("data", "")))
    if not m:
        return
    approval_id, action = int(m.group(1)), m.group(2)
    approval = await approvals.get(approval_id)
    if approval is None or approval.user_id != event.user_id:
        return
    if action == "edit":
        if await approvals.claim(approval_id, {ApprovalStatus.PENDING}, ApprovalStatus.AWAITING_EDIT):
            await say(event.user_id, EDIT_QUESTION)
        return
    if not await approvals.claim(approval_id, _OPENABLE, ApprovalStatus.RESOLVING):
        # Already handled (double tap, Telegram retry, or an old prompt). Say so once.
        if ApprovalStatus(approval.status) in _DONE:
            await say(event.user_id, "That one's already been handled.",
                      dedupe_key=f"approval:{approval_id}:handled")
        return
    decision = "ok" if action == "ok" else "no"
    await audit.record(event.user_id, actor="user", action=f"approval.{decision}",
                       detail={"approval_id": approval_id, "via": "button"})
    await _resume(approval, decision)


def quick_decision(text: str) -> ApprovalReplyInterpretation | None:
    """Classify plain answers such as "ok", "send it" or "cancel" without a model call (else None)."""
    key = _QUICK_STRIP.sub(" ", (text or "").lower().replace("’", "'")).strip()
    if not key and (text or "").strip() in ("👍", "✅"):
        key = "ok"
    decision = _QUICK.get(key)
    return ApprovalReplyInterpretation(decision=decision) if decision else None


async def interpret_reply(approval, text: str) -> ApprovalReplyInterpretation:
    if (quick := quick_decision(text)) is not None:
        return quick
    if approval.status == ApprovalStatus.AWAITING_EDIT:
        return ApprovalReplyInterpretation(decision="edit", instructions=text)
    return await llm.structured(
        ApprovalReplyInterpretation, INTERPRET_PROMPT,
        f"Pending action:\n{approval.preview}\n\nUser's reply:\n{text}", tier=llm.Tier.FAST,
    )


async def _which_one(approval) -> str | None:
    """A text reply is only a decision when exactly one approval is waiting on the user.

    The exception is a single approval in AWAITING_EDIT: the user tapped Edit on it, so the next
    message is its change request. Returns the "which one?" question when it is ambiguous."""
    waiting = [a for a in await approvals.open_for_user(approval.user_id)
               if ApprovalStatus(a.status) in _OPENABLE]
    if len(waiting) <= 1:
        return None
    editing = [a for a in waiting if a.status == ApprovalStatus.AWAITING_EDIT]
    if len(editing) == 1 and editing[0].id == approval.id:
        return None
    lines = [f"{i}. {sanitize_line(a.preview.splitlines()[0] if a.preview else a.tool)[:120]}"
             for i, a in enumerate(waiting, 1)]
    return ("I have more than one thing waiting on you, so which one do you mean? "
            "Tap the buttons on the one you want.\n" + "\n".join(lines))


async def apply_reply(approval, interp: ApprovalReplyInterpretation) -> str | None:
    if interp.decision == "unrelated":
        return None
    if (ask := await _which_one(approval)) is not None:
        return ask
    if not await approvals.claim(approval.id, _OPENABLE, ApprovalStatus.RESOLVING):
        return "That one's already been handled."
    decision = {"approve": "ok", "cancel": "no", "edit": "edit"}[interp.decision]
    await audit.record(approval.user_id, actor="user", action=f"approval.{decision}",
                       detail={"approval_id": approval.id, "via": "text"})
    await _resume(approval, decision, interp.instructions)
    return {
        "ok": "On it.",
        "no": "Okay, cancelled.",
        "edit": "Got it, revising. I'll show you the new version.",
    }[decision]


async def remind(user_id: int, approval_id: int) -> None:
    approval = await approvals.get(approval_id)
    if approval is None or approval.user_id != user_id or approval.status != ApprovalStatus.PENDING:
        return
    text = f"Still want me to go ahead with this? It expires in about 2 hours.\n\n{approval.preview}"
    await say(user_id, text, approval_buttons(approval_id), dedupe_key=f"approval:{approval_id}:remind",
              tainted=await _task_tainted(approval))


async def expire(user_id: int, approval_id: int) -> None:
    approval = await approvals.get(approval_id)
    if approval is None or approval.user_id != user_id:
        return
    if await approvals.claim(approval_id, _OPENABLE, ApprovalStatus.RESOLVING):
        await _resume(approval, "expired")


async def on_remind_wakeup(user_id: int, reason: str) -> None:
    """System wakeup handler for kind system_approval_remind (reason is `approval:<id>`)."""
    if (approval_id := approval_id_from_reason(reason)) is not None:
        await remind(user_id, approval_id)
    await sweep(user_id)


async def on_expire_wakeup(user_id: int, reason: str) -> None:
    """System wakeup handler for kind system_approval_expire."""
    if (approval_id := approval_id_from_reason(reason)) is not None:
        await expire(user_id, approval_id)
    await sweep(user_id)


# --- sweep ------------------------------------------------------------------------------


def _preview_lines(approval) -> str:
    return "\n".join(sanitize_line(line) for line in (approval.preview or "").splitlines())


async def _sweep_stuck_resolving(cutoff, user_id: int | None) -> int:
    from mavis.agents import orchestrator  # lazy: the runner imports this module

    n = 0
    for ap in await approvals.stuck_resolving(cutoff, user_id):
        task = await tasks.get(ap.task_id) if ap.task_id is not None else None
        if task is not None and task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            continue  # the task is alive; its own timeout and reaper deal with it
        if task is not None and task.status == TaskStatus.AWAITING_APPROVAL:
            # The decision was recorded but the resume never ran, and the decision itself is not
            # kept, so replaying a guess is unsafe. Fail the task (this closes the approval too).
            await orchestrator.fail_task(task.id, ap.user_id, "I lost your answer before I could act on it")
        elif await approvals.set_status(ap.id, ApprovalStatus.FAILED, "the decision was never applied",
                                        from_statuses={ApprovalStatus.RESOLVING}):
            await say(ap.user_id, "I couldn't finish acting on your answer here, so nothing was done:\n"
                      + _preview_lines(ap), dedupe_key=f"approval:{ap.id}:never_ran")
        n += 1
    return n


async def _sweep_may_have_run(user_id: int | None) -> int:
    cutoff = utcnow() - timedelta(seconds=get_settings().task_timeout_s + 60)
    n = 0
    for ap in await approvals.stale_may_have_run(cutoff, user_id):
        await say(ap.user_id, "One thing to check: this may have gone through before I lost track of it.\n"
                  + _preview_lines(ap), dedupe_key=f"approval:{ap.id}:may_have_run")
        if await approvals.close_may_have_run(ap.id, "may have gone through; the outcome was not recorded"):
            n += 1
    return n


async def _sweep_after_stop(user_id: int | None) -> int:
    """A task cancelled while it waited (or failed later) after an approval already ran."""
    n = 0
    for ap in await approvals.executed_after_stop(utcnow() - _AFTER_STOP_WINDOW, user_id):
        await say(ap.user_id, "Heads up: this went through before the task stopped.\n" + _preview_lines(ap),
                  dedupe_key=f"approval:{ap.id}:ran_after_stop")
        n += 1
    return n


async def _sweep_overdue(user_id: int | None) -> int:
    n = 0
    for ap in await approvals.overdue_open(utcnow(), user_id):
        await expire(ap.user_id, ap.id)
        n += 1
    return n


STARTUP_SKIPPED_STEPS = frozenset({"stuck"})


async def sweep(user_id: int | None = None, *, skip: frozenset[str] = frozenset()) -> dict[str, int]:
    """Repair approvals the live path could not finish. Safe to run any time and repeatedly.

    Runs at worker start, with the morning check-in, and whenever an approval reminder or expiry
    wakeup fires. Each step is isolated so one failure does not hide the rest. `skip` names steps
    to leave out."""
    from mavis.agents import orchestrator  # lazy: the runner imports this module

    steps = {
        # first: a task stuck RUNNING or QUEUED without a job holds up the approvals behind it
        "tasks": lambda: orchestrator.recover_tasks(user_id),
        "expired": lambda: _sweep_overdue(user_id),
        "stuck": lambda: _sweep_stuck_resolving(utcnow() - STALE_AFTER, user_id),
        "may_have_run": lambda: _sweep_may_have_run(user_id),
        "ran_after_stop": lambda: _sweep_after_stop(user_id),
    }
    out: dict[str, int] = {}
    for name, fn in steps.items():
        if name in skip:
            continue
        try:
            out[name] = await fn()
        except Exception as exc:  # noqa: BLE001 - keep sweeping
            log.warning("approval.sweep_failed", step=name, error=type(exc).__name__)
            out[name] = 0
    return out


async def sweep_approvals() -> None:
    """Worker start hook. It skips the "stuck RESOLVING" step: after a worker outage longer than
    STALE_AFTER, the row's RESUME_TASK job may still be queued in the bus and about to run, so failing
    the task here would drop the user's decision. The morning and wakeup sweeps cover that step."""
    await sweep(skip=STARTUP_SKIPPED_STEPS)


async def sweep_for_user(user_id: int) -> None:
    """Morning check-in hook."""
    await sweep(user_id)


def register_sweeps() -> None:
    """Hook the sweep into worker start and the daily check-in, and the two approval wakeups.

    Idempotent: module-level functions, so the hook lists dedupe them on every call."""
    from mavis.initiative import routines
    from mavis.timers.system import register_system_wakeup
    from mavis.worker.runner import register_startup_hook

    register_startup_hook(sweep_approvals)
    routines.register_morning_hook(sweep_for_user)
    register_system_wakeup("system_approval_remind", on_remind_wakeup)
    register_system_wakeup("system_approval_expire", on_expire_wakeup)
