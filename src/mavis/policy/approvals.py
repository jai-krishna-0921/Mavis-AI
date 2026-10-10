"""Approval UX: prompts with buttons, button/text decisions, reminders and expiry.

Decisions never execute anything here. They move the approval to RESOLVING
(atomically, so double taps are harmless) and enqueue RESUME_TASK; the
orchestrator's approval_gate performs the action.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import structlog
from pydantic import BaseModel

from mavis import bus
from mavis.channels.formatting import verbatim
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.events import Event, Job, JobKind
from mavis.domain.messages import TAINT_SUFFIX, Button, Outbound, Role
from mavis.domain.tasks import ApprovalStatus, TaskStatus
from mavis.domain.wakeups import WakeupKind
from mavis.llm import models as llm
from mavis.store.db import Session, utcnow
from mavis.store.repo import approvals, audit, messages, outbox, tasks, users
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


PAST_ACTION_TEXT = ("That was for {when}, which has already passed, so I didn't do it. "
                    "Ask me again with a new time if you still want it.")


def _declared(tool_name: str):
    """The tool's own declaration (identity, action time); None for a tool no longer registered."""
    from mavis.tools.registry import get_registry  # lazy: the registry imports the approvals repo

    return get_registry().find(tool_name)


def identity_of(tool_name: str) -> tuple[str, ...]:
    tool = _declared(tool_name)
    return tool.identity if tool is not None else ()


def action_time(approval, timezone: str) -> datetime | None:
    """When the approved action takes effect, from the argument its tool declares (a naive value is
    read in the user's timezone, as the tool itself reads it). None when it declares none."""
    tool = _declared(approval.tool)
    field = tool.action_time if tool is not None else None
    raw = (approval.arguments or {}).get(field) if field else None
    if isinstance(raw, datetime):
        at = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            at = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return timeutil.to_utc(at, timezone) if at.tzinfo is None else at.astimezone(UTC)


async def passed_action_time(approval) -> str | None:
    """The action's own time as the user reads it, if that time has already passed; else None."""
    try:
        timezone = (await users.get(approval.user_id)).timezone
    except Exception:  # noqa: BLE001 - no user, no timezone: treat naive times as UTC
        timezone = "UTC"
    at = action_time(approval, timezone)
    if at is None or at > utcnow():
        return None
    return f"{timeutil.to_local(at, timezone):%a %d %b %H:%M}"


async def send_approval_prompt(user_id: int, payload: dict) -> None:
    approval = await approvals.get(int(payload["approval_id"]))
    if approval is None or approval.status != ApprovalStatus.PENDING:
        return
    # the card shows the preview verbatim: what the user approves is exactly what the action does
    text = f"Ready when you are. Want me to go ahead?\n\n{verbatim(approval.preview)}"
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
    await sync_action_time_expiry(approval)  # every prompt, so an edited time is followed


def action_time_reason(approval_id: int) -> str:
    return f"approval:{approval_id}:action_time"


async def sync_action_time_expiry(approval) -> None:
    """Keep exactly one pending expiry wakeup at the action time of the approval's CURRENT arguments:
    cancel any for an older time, schedule one for the current time if it is still ahead."""
    tool = _declared(approval.tool)
    if tool is None or tool.action_time is None:
        return  # the tool's actions have no time of their own
    try:
        timezone = (await users.get(approval.user_id)).timezone
    except Exception:  # noqa: BLE001
        timezone = "UTC"
    at = action_time(approval, timezone)
    wanted = at if at is not None and at > utcnow() else None
    wakeups = timers_service.WakeupService()
    reason = action_time_reason(approval.id)
    kept = False
    for w in await wakeups.pending(approval.user_id, WakeupKind.SYSTEM_APPROVAL_EXPIRE):
        if w.reason != reason:
            continue
        if wanted is not None and w.due_at == wanted and not kept:
            kept = True
        else:
            await wakeups.cancel(w.id)
    if wanted is not None and not kept:
        await wakeups.wake_me(approval.user_id, wanted, reason, kind=WakeupKind.SYSTEM_APPROVAL_EXPIRE,
                              scale=False, dedupe_key=f"{reason}:{wanted.isoformat()}")


async def expire_at_action_time(user_id: int, approval_id: int) -> None:
    """The action-time wakeup fired: expire only a card still waiting on a tap whose CURRENT action time
    has passed. A card being edited, or one whose time was moved, is left alone."""
    approval = await approvals.get(approval_id)
    if approval is None or approval.user_id != user_id or approval.status != ApprovalStatus.PENDING:
        return
    if await passed_action_time(approval) is None:
        return
    if await approvals.claim(approval_id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING):
        await _resume(approval, "past")


# --- decisions ------------------------------------------------------------------------

_BUTTON = re.compile(r"^ap:(\d+):(ok|edit|no)$")
_REASON = re.compile(r"^approval:(\d+)$")
_ACTION_REASON = re.compile(r"^approval:(\d+):action_time$")
_OPENABLE = {ApprovalStatus.PENDING, ApprovalStatus.AWAITING_EDIT}
_DONE = {ApprovalStatus.EXECUTED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.FAILED}

INTERPRET_PROMPT = """The user was shown a pending action and asked to approve it.
Their reply was not a plain yes.
Classify it:
- cancel: they don't want it ("no", "don't", "cancel")
- edit: they want changes to the action; put the requested change in `instructions`
- unrelated: anything else, including questions about the action and anything that sounds like a yes
Never answer approve: approving takes the button or a plain yes."""

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
        final = {"no": ApprovalStatus.REJECTED, "expired": ApprovalStatus.EXPIRED,
                 "past": ApprovalStatus.EXPIRED}.get(decision, ApprovalStatus.FAILED)
        await approvals.set_status(approval.id, final, "no task to resume",
                                   from_statuses={ApprovalStatus.RESOLVING})
        if decision == "past":
            when = await passed_action_time(approval) or "a time that has passed"
            text = PAST_ACTION_TEXT.format(when=when) + "\n\n" + _preview_lines(approval)
            await say(approval.user_id, text, dedupe_key=f"approval:{approval.id}:past",
                      tainted=bool(approval.tainted))
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


SUPERSEDED_NOTE = "superseded by approval #{id}"


async def supersede_duplicates(decided, *, executed: bool) -> int:
    """`decided` was just executed or rejected: close this user's other cards for the same action
    (same tool, same declared identity) so no stale duplicate stays approvable. Once an action has
    executed, every open twin is evidence of an action already done, whatever its taint; a rejection
    closes only twins of the same taint. A task waiting on that card gets a "superseded" decision (its
    gate closes it without running anything); any other duplicate (its task has not reached the gate
    yet, or there is no task) is closed in place."""
    n = 0
    for dup in await approvals.waiting_equivalents(
            decided.user_id, decided.tool, decided.arguments or {}, identity=identity_of(decided.tool),
            tainted=None if executed else bool(decided.tainted), exclude_id=decided.id):
        note = SUPERSEDED_NOTE.format(id=decided.id)
        task = await tasks.get(dup.task_id) if dup.task_id is not None else None
        nxt = await approvals.next_open(task.id) if task is not None else None
        waiting_on_it = (task is not None and task.status == TaskStatus.AWAITING_APPROVAL
                         and nxt is not None and nxt.id == dup.id)
        if waiting_on_it:
            if await approvals.claim(dup.id, _OPENABLE, ApprovalStatus.RESOLVING):
                await _resume(dup, "superseded")
                n += 1
        elif await approvals.set_status(dup.id, ApprovalStatus.REJECTED, note, from_statuses=_OPENABLE):
            n += 1
    if n:
        log.info("approval.duplicates_superseded", approval_id=decided.id, count=n)
    if executed:
        await _note_versions_sent(decided)
    return n


VERSION_SENT_TEXT = ("A version of this was already sent. This one is different and still waiting, in case "
                     "you want it too:\n\n{preview}")


async def _note_versions_sent(decided) -> None:
    """Open cards for the same target with different content stay open (they may be a real, different
    action), but the user is told a version already went out, with the card's buttons."""
    tool = _declared(decided.tool)
    for card in await approvals.waiting_same_target(
            decided.user_id, decided.tool, decided.arguments or {}, target=tool.target if tool else (),
            tainted=None, statuses=[ApprovalStatus.PENDING], exclude_id=decided.id):
        await say(card.user_id, VERSION_SENT_TEXT.format(preview=verbatim(card.preview)),
                  approval_buttons(card.id), dedupe_key=f"approval:{card.id}:version_sent:{decided.id}",
                  tainted=bool(card.tainted))


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


# A short message made ONLY of these words is a plain yes / no ("yes go ahead", "sounds good, do it"). Any
# other word ("but", "make", a name, a question) makes it a message for the model, never an approval.
_YES_WORDS = frozenset(
    "yes yeah yep yup y ya ok okay k kk sure please pls go ahead do it that this send approve approved "
    "ship sounds looks good great perfect fine cool thanks thank you definitely absolutely of course "
    "lets let's let proceed confirm confirmed works right and for on now then".split())
_NO_WORDS = frozenset(
    "no nope nah n cancel stop dont don't do not never mind nevermind forget skip it that this please "
    "thanks thank you rather".split())
_NO_DECISIVE = frozenset("no nope nah n cancel stop dont don't not never nevermind forget skip".split())
_YES_DECISIVE = frozenset(
    "yes yeah yep yup y ya ok okay k kk sure go ahead approve approved proceed confirm confirmed ship "
    "sounds good looks great perfect fine works absolutely definitely".split())
_QUICK_MAX_WORDS = 6


def quick_decision(text: str) -> ApprovalReplyInterpretation | None:
    """Classify plain answers such as "ok", "yes go ahead" or "cancel" without a model call (else None)."""
    key = _QUICK_STRIP.sub(" ", (text or "").lower().replace("’", "'")).strip()
    if not key and (text or "").strip() in ("👍", "✅"):
        key = "ok"
    decision = _QUICK.get(key)
    if decision is None:
        words = re.findall(r"[a-z']+", key)
        if words and len(words) <= _QUICK_MAX_WORDS and not re.search(r"[?]", text or ""):
            if all(w in _YES_WORDS for w in words) and any(w in _YES_DECISIVE for w in words) \
                    and not any(w in _NO_DECISIVE for w in words):
                decision = "approve"
            elif all(w in _NO_WORDS for w in words) and any(w in _NO_DECISIVE for w in words):
                decision = "cancel"
    return ApprovalReplyInterpretation(decision=decision) if decision else None


async def interpret_reply(approval, text: str) -> ApprovalReplyInterpretation:
    if (quick := quick_decision(text)) is not None:
        return quick
    if approval.status == ApprovalStatus.AWAITING_EDIT:
        return ApprovalReplyInterpretation(decision="edit", instructions=text)
    # The model never approves: only a plain short answer (above) or the button does. It never sees the
    # preview either, which may carry third-party text written to steer it; it gets the tool name only.
    interp = await llm.structured(
        ApprovalReplyInterpretation, INTERPRET_PROMPT,
        f"Pending action: a {approval.tool} action (details withheld)\n\nUser's reply:\n{text}",
        tier=llm.Tier.FAST,
    )
    if interp.decision == "approve":
        return ApprovalReplyInterpretation(decision="unrelated")
    return interp


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
    lines = [f"{i}. {verbatim((a.preview.splitlines()[0] if a.preview else a.tool)[:120])}"
             for i, a in enumerate(waiting, 1)]
    return ("I have more than one thing waiting on you, so which one do you mean? "
            "Tap the buttons on the one you want.\n" + "\n".join(lines))


async def apply_reply(approval, interp: ApprovalReplyInterpretation) -> str | None:
    """The acknowledgement to send now, "" when the decision's own receipt follows, None when unrelated."""
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
    # A cancel says nothing here: the resumed task's own receipt ("Okay, not doing that.") is the one reply.
    return {
        "ok": "On it.",
        "no": "",
        "edit": "Got it, revising. I'll show you the new version.",
    }[decision]


async def remind(user_id: int, approval_id: int) -> None:
    approval = await approvals.get(approval_id)
    if approval is None or approval.user_id != user_id or approval.status != ApprovalStatus.PENDING:
        return
    if await passed_action_time(approval) is not None:  # nothing to remind about: it can only expire
        await expire(user_id, approval_id)
        return
    text = ("Still want me to go ahead with this? It expires in about 2 hours.\n\n"
            f"{verbatim(approval.preview)}")
    await say(user_id, text, approval_buttons(approval_id), dedupe_key=f"approval:{approval_id}:remind",
              tainted=await _task_tainted(approval))


async def expire(user_id: int, approval_id: int) -> None:
    approval = await approvals.get(approval_id)
    if approval is None or approval.user_id != user_id:
        return
    if await approvals.claim(approval_id, _OPENABLE, ApprovalStatus.RESOLVING):
        await _resume(approval, "past" if await passed_action_time(approval) else "expired")


async def on_remind_wakeup(user_id: int, reason: str) -> None:
    """System wakeup handler for kind system_approval_remind (reason is `approval:<id>`)."""
    if (approval_id := approval_id_from_reason(reason)) is not None:
        await remind(user_id, approval_id)
    await sweep(user_id)


async def on_expire_wakeup(user_id: int, reason: str) -> None:
    """System wakeup handler for kind system_approval_expire (the TTL, or the action's own time)."""
    if (m := _ACTION_REASON.match(reason or "")) is not None:
        await expire_at_action_time(user_id, int(m.group(1)))
    elif (approval_id := approval_id_from_reason(reason)) is not None:
        await expire(user_id, approval_id)
    await sweep(user_id)


# --- sweep ------------------------------------------------------------------------------


def _preview_lines(approval) -> str:
    return verbatim(approval.preview or "")


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
                                        from_statuses={ApprovalStatus.RESOLVING},
                                        failure_reason="your answer was never applied"):
            from mavis.policy import outcomes  # lazy: outcomes imports the registry

            await outcomes.block_loops_of_failed_approval(ap.id)
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


async def _sweep_past_action_time(user_id: int | None) -> int:
    """PENDING approvals whose action's own time has passed (an event that already started). Cards the
    user is editing are left alone (they may be giving a new time); the gate refuses them anyway."""
    n = 0
    for ap in await approvals.pending_rows(user_id):
        if await passed_action_time(ap) is not None:
            await expire(ap.user_id, ap.id)
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
        "past": lambda: _sweep_past_action_time(user_id),
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
