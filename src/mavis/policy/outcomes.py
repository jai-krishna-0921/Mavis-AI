"""Truthful outcomes (hotfix4 H1): what recently failed, computed from the rows, for every reader.

`recently_failed` is the ONE source for "Recently failed (needs your decision)": the `pending` tool and the
morning brief use it, and the reasoner and ping composers should too. An item is a failed approval (an
action the user approved that did not happen) or a background task that ended FAILED or PARTIAL, in the
last 48 hours, until the user acknowledges it (`acknowledge`) or the same subject succeeds later.

A failed approval also blocks the loops LEARN wrote from its chat turn and the turn before it: the link is
structural (the APPROVAL task's turn id and the message order), never title text. A blocked loop is not
OPEN, so no prep, reminder or "how did it go" fires for something that never happened.

Phase B (commitments ledger) replaces loops/approvals/tasks with ledger items; this is the minimal rule at
the current layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.loops import LoopOrigin, LoopStatus
from mavis.domain.tasks import ApprovalStatus, TaskKind, TaskStatus
from mavis.domain.timefmt import relative_past
from mavis.policy.risk import wrap_untrusted
from mavis.store.repo import approvals, messages, tasks
from mavis.store.repo import loops as loops_repo

log = structlog.get_logger()

WINDOW = timedelta(hours=48)
HEADING = "Recently failed (needs your decision)"
NOT_THROUGH = "it did not go through"
TASK_FAILED = "it could not be finished"
TASK_PARTIAL = "only part of it got done"


@dataclass(frozen=True)
class FailedItem:
    ref: str  # "approval:9" or "task:14": what `acknowledge` takes
    summary: str  # code-rendered: the approval card's first line, or the task's goal
    outcome: str  # "failed" or "partly done"
    reason: str  # plain words (FailureKind text or a fixed sentence), never a provider body
    at: datetime
    tainted: bool  # the summary may carry third-party text


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _same_subject(failed, done) -> bool:
    """Did `done` (a later executed approval of the same tool) do what `failed` tried? Same target, same
    action time, or the same action (the tool's identity arguments)."""
    if failed.tool != done.tool:
        return False
    from mavis.tools.registry import get_registry  # lazy: the registry loads every tool module

    tool = get_registry().find(failed.tool)
    a, b = failed.arguments or {}, done.arguments or {}
    if tool is not None:
        if tool.target and (key := approvals.target_key(a, tool.target)) is not None:
            if key == approvals.target_key(b, tool.target):
                return True
        when = tool.action_time
        if when and a.get(when) and b.get(when) and \
                approvals.equivalence_key(a, (when,)) == approvals.equivalence_key(b, (when,)):
            return True  # the same tool acting at the same moment, after the failure: the retry
        return approvals.equivalent(a, b, tool.identity)
    return approvals.equivalent(a, b)


async def recently_failed(user_id: int, now: datetime | None = None) -> list[FailedItem]:
    """Failed approvals and FAILED/PARTIAL background tasks of the last 48 h, oldest first, that the user
    has not acknowledged and that no later success of the same subject has replaced."""
    now = now or timeutil.now()
    since = now - WINDOW
    items: list[FailedItem] = []
    executed = await approvals.resolved_since(user_id, since, [ApprovalStatus.EXECUTED])
    for ap in await approvals.resolved_since(user_id, since, [ApprovalStatus.FAILED]):
        if ap.acknowledged_at is not None:
            continue
        resolved = timeutil.ensure_utc(ap.resolved_at)
        if any(_same_subject(ap, e) and timeutil.ensure_utc(e.resolved_at) > resolved for e in executed):
            continue
        task = await tasks.get(ap.task_id) if ap.task_id is not None else None
        if task is not None and task.kind == TaskKind.TASK.value:
            continue  # a background task's approval is reported with its task (PARTIAL or FAILED)
        summary = ((ap.preview or ap.tool).splitlines() or [ap.tool])[0][:160]
        items.append(FailedItem(f"approval:{ap.id}", summary, "failed", ap.failure_reason or NOT_THROUGH,
                                resolved, bool(ap.tainted)))
    done = await tasks.finished_since(user_id, since, [TaskStatus.DONE], kind=TaskKind.TASK)
    outcomes = await tasks.finished_since(user_id, since, [TaskStatus.FAILED, TaskStatus.PARTIAL],
                                          kind=TaskKind.TASK)
    for t in outcomes:
        if t.acknowledged_at is not None:
            continue
        finished = timeutil.ensure_utc(t.finished_at)
        if any(_norm(d.goal) == _norm(t.goal) and timeutil.ensure_utc(d.finished_at) > finished
               for d in done):
            continue  # asked again and that run finished
        partial = t.status == TaskStatus.PARTIAL.value
        items.append(FailedItem(
            f"task:{t.id}", t.goal[:160], "partly done" if partial else "failed",
            t.error or (TASK_PARTIAL if partial else TASK_FAILED), finished, bool(t.tainted)))
    return sorted(items, key=lambda i: i.at)


def render_recently_failed(items: list[FailedItem], now: datetime, tz: str,
                           source: str = "outcomes") -> tuple[str, bool]:
    """(section text, whether any third-party text is in it). Empty text when nothing failed."""
    if not items:
        return "", False
    lines, untrusted = [], False
    for i in items:
        summary = i.summary
        if i.tainted:
            summary, untrusted = wrap_untrusted(summary, source), True
        lines.append(f"- {summary}: {i.outcome}, {i.reason} ({relative_past(i.at, now, tz)}) [{i.ref}]")
    return f"{HEADING}:\n" + "\n".join(lines), untrusted


async def acknowledge(user_id: int, refs: list[str]) -> int:
    """The user has seen and decided on these items ("approval:9", "task:14"): stop showing them."""
    ids: dict[str, list[int]] = {"approval": [], "task": []}
    for ref in refs:
        kind, _, num = ref.strip().partition(":")
        if kind in ids and num.strip().isdigit():
            ids[kind].append(int(num))
    acked = await approvals.acknowledge(user_id, ids["approval"])
    if acked:
        await drop_blocked_by(user_id, ids["approval"])
    return acked + await tasks.acknowledge(user_id, ids["task"])


# --- loops of a failed action ---------------------------------------------------------------------


def _ref(approval_id: int) -> str:
    return f"approval:{approval_id}"


async def _turns_of(approval) -> list[str]:
    """The chat turn that queued `approval`, plus the user's turn just before it (whose request it usually
    answered) when that came within `failed_turn_link_minutes`. An older previous turn is unrelated."""
    task = await tasks.get(approval.task_id) if approval.task_id is not None else None
    if task is None or task.kind != TaskKind.APPROVAL.value or not task.turn_ref:
        return []
    turns = [task.turn_ref]
    window = timedelta(minutes=get_settings().failed_turn_link_minutes)
    previous = await messages.previous_user_event(approval.user_id, task.turn_ref)
    if previous is not None and previous[1] <= window:
        turns.append(previous[0])
    return turns


async def failed_turns(user_id: int, now: datetime | None = None) -> dict[str, int]:
    """Chat turns linked to an approval that failed in the last 48 h: turn id -> approval id."""
    since = (now or timeutil.now()) - WINDOW
    turns: dict[str, int] = {}
    for ap in await approvals.resolved_since(user_id, since, [ApprovalStatus.FAILED]):
        if ap.acknowledged_at is not None:
            continue  # the user already decided about it
        for turn in await _turns_of(ap):
            turns.setdefault(turn, ap.id)
    return turns


async def block_loops_of_failed_approval(approval_id: int) -> int:
    """Move the OPEN conversation loops CREATED in a failed approval's turns to BLOCKED. Returns how many."""
    approval = await approvals.get(approval_id)
    if approval is None or approval.status != ApprovalStatus.FAILED.value:
        return 0
    turns = await _turns_of(approval)
    return await _set(approval.user_id, await loops_repo.list_created_in(approval.user_id, turns,
                                                                        (LoopStatus.OPEN,)),
                      LoopStatus.BLOCKED, blocked_by=_ref(approval.id))


async def drop_loops_of_rejected_approval(approval_id: int) -> int:
    """The user said no to an action: the OPEN conversation loops created in its turns (the request it
    answered, "send Ravi the invite at 3") are dropped, so nothing later says it is still waiting. Same
    structural link as a failure (turn ids, never titles). Returns how many."""
    approval = await approvals.get(approval_id)
    if approval is None or approval.status != ApprovalStatus.REJECTED.value:
        return 0
    turns = await _turns_of(approval)
    created = [lp for lp in await loops_repo.list_created_in(approval.user_id, turns, (LoopStatus.OPEN,))
               if lp.origin is LoopOrigin.CONVERSATION]
    return await _set(approval.user_id, created, LoopStatus.DROPPED)


async def block_if_from_failed_turn(user_id: int, loop) -> bool:
    """A loop LEARN created after the approval of its turn had already failed: block it at once. A loop
    that only merged into an older one (created elsewhere) is left alone."""
    if not loop.created_ref or loop.status is not LoopStatus.OPEN:
        return False
    approval_id = (await failed_turns(user_id)).get(loop.created_ref)
    if approval_id is None:
        return False
    return await _set(user_id, [loop], LoopStatus.BLOCKED, blocked_by=_ref(approval_id)) > 0


async def reopen_after_success(approval_id: int) -> int:
    """An approved action executed: loops blocked by an earlier failure of the same action (same target,
    action time or identity, see _same_subject) go back to OPEN."""
    done = await approvals.get(approval_id)
    if done is None or done.status != ApprovalStatus.EXECUTED.value:
        return 0
    since = timeutil.now() - WINDOW
    failed = [ap for ap in await approvals.resolved_since(done.user_id, since, [ApprovalStatus.FAILED])
              if ap.id != done.id and _same_subject(ap, done)]
    blocked = await loops_repo.list_blocked_by(done.user_id, [_ref(ap.id) for ap in failed])
    return await _set(done.user_id, blocked, LoopStatus.OPEN)


async def drop_blocked_by(user_id: int, approval_ids: list[int]) -> int:
    """The user decided about a failure (acknowledged it): the loops it blocked are dropped."""
    blocked = await loops_repo.list_blocked_by(user_id, [_ref(i) for i in approval_ids])
    return await _set(user_id, blocked, LoopStatus.DROPPED)


# --- loops of delivered work ----------------------------------------------------------------------

DELIVERED_KEY = "delivered_turns"  # users.state: {turn id: {"at": iso time, "what": the file}}
DELIVERED_WINDOW = timedelta(hours=12)
# a loop from another turn is about making a file only when it names one ("pitch deck retry requested")
FILE_WORDS = frozenset("deck slides slide presentation powerpoint pptx doc docs document report proposal "
                       "agenda notes sheet sheets spreadsheet excel xlsx tracker budget pdf file".split())


def _about(loop, what: str) -> bool:
    """Is `loop` about the file described by `what`? It shares a word with it (the turn link already says
    they are related; this keeps "buy milk" from the same turn open), and it is not due later."""
    due = timeutil.ensure_utc(loop.due_at) if loop.due_at else None
    if due is not None and due > timeutil.now():
        return False  # "review the deck on Friday" is still ahead
    return bool(set(loops_repo.title_tokens(loop.title)) & set(loops_repo.title_tokens(what)))


async def delivered(user_id: int, turn_ref: str | None, what: str) -> int:
    """Mavis made and sent a file (`what`: its title and kind) for the chat turn `turn_ref`. The OPEN
    conversation loops LEARN wrote about making it ("create the pitch deck", "deck retry requested") are
    DONE: those created in that turn or the one before it (structural, as for a failure), and those of the
    last hours that name the same matter (the asks of earlier attempts that failed). The turn is recorded
    so a loop LEARN writes from it afterwards is closed on creation (done_if_from_delivered_turn)."""
    now = timeutil.now()
    turns: list[str] = []
    if turn_ref:
        turns = [turn_ref]
        previous = await messages.previous_user_event(user_id, turn_ref)
        if previous is not None and previous[1] <= timedelta(minutes=get_settings().failed_turn_link_minutes):
            turns.append(previous[0])

        def record(cur: dict) -> dict:
            keep = {k: v for k, v in cur.items() if isinstance(v, dict) and
                    now - datetime.fromisoformat(v.get("at", "1970-01-01T00:00:00+00:00")) <= WINDOW}
            return {**keep, turn_ref: {"at": now.isoformat(), "what": what[:200]}}

        from mavis.store.repo import users  # lazy: keeps this module's import surface small

        await users.modify_nested(user_id, DELIVERED_KEY, record)
    since = now - DELIVERED_WINDOW
    done = []
    for lp in await loops_repo.list_open(user_id):
        if lp.origin is not LoopOrigin.CONVERSATION or not _about(lp, what):
            continue
        if lp.created_ref in turns:
            done.append(lp)
        elif lp.created_at and timeutil.ensure_utc(lp.created_at) >= since and \
                FILE_WORDS & set(loops_repo.title_tokens(lp.title)) and \
                loops_repo.same_matter(lp.title, what, one_word=False):
            done.append(lp)
    return await _set(user_id, done, LoopStatus.DONE)


async def done_if_from_delivered_turn(user_id: int, loop) -> bool:
    """A loop LEARN created from a turn whose file was already made and sent: close it at once."""
    if not loop.created_ref or loop.status is not LoopStatus.OPEN or \
            loop.origin is not LoopOrigin.CONVERSATION:
        return False
    from mavis.store.repo import users

    entry = ((await users.get_state(user_id)).get(DELIVERED_KEY) or {}).get(loop.created_ref)
    if not isinstance(entry, dict) or not _about(loop, str(entry.get("what", ""))):
        return False
    return await _set(user_id, [loop], LoopStatus.DONE) > 0


async def _set(user_id: int, loops: list, status: LoopStatus, blocked_by: str | None = None) -> int:
    from mavis import bus  # lazy: bus wiring imports the worker
    from mavis.loops.service import LoopService

    service = LoopService(bus.get_bus())
    changed = 0
    for lp in loops:
        if status is LoopStatus.BLOCKED and lp.origin is not LoopOrigin.CONVERSATION:
            continue  # only loops the user's own turn produced; the reasoner's are its business
        if await service.close(lp.id, status, blocked_by=blocked_by) is not None:
            changed += 1
    if changed:
        log.info("outcomes.loops_set", user_id=user_id, status=status.value, count=changed)
    return changed
