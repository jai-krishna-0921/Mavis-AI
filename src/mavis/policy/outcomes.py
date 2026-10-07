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
    return acked + await tasks.acknowledge(user_id, ids["task"])


# --- loops of a failed action ---------------------------------------------------------------------


async def _turns_of(approval) -> list[str]:
    """The chat turn that queued `approval` and the one before it (whose request it usually answered)."""
    task = await tasks.get(approval.task_id) if approval.task_id is not None else None
    if task is None or task.kind != TaskKind.APPROVAL.value or not task.turn_ref:
        return []
    turns = [task.turn_ref]
    if (previous := await messages.previous_user_event(approval.user_id, task.turn_ref)) is not None:
        turns.append(previous)
    return turns


async def failed_turns(user_id: int, now: datetime | None = None) -> set[str]:
    """Chat turns linked to an approval that failed in the last 48 h."""
    since = (now or timeutil.now()) - WINDOW
    turns: set[str] = set()
    for ap in await approvals.resolved_since(user_id, since, [ApprovalStatus.FAILED]):
        turns.update(await _turns_of(ap))
    return turns


async def block_loops_of_failed_approval(approval_id: int) -> int:
    """Move the OPEN conversation loops of a failed approval's turns to BLOCKED. Returns how many."""
    approval = await approvals.get(approval_id)
    if approval is None or approval.status != ApprovalStatus.FAILED.value:
        return 0
    turns = await _turns_of(approval)
    return await _block(approval.user_id, turns)


async def block_if_from_failed_turn(user_id: int, loop_id: int, source: str) -> bool:
    """A loop LEARN wrote after the approval had already failed: block it at once."""
    if source not in await failed_turns(user_id):
        return False
    return await _block(user_id, [source], only=loop_id) > 0


async def _block(user_id: int, turns: list[str], only: int | None = None) -> int:
    if not turns:
        return 0
    from mavis import bus  # lazy: bus wiring imports the worker
    from mavis.loops.service import LoopService

    service = LoopService(bus.get_bus())
    blocked = 0
    for lp in await loops_repo.list_from_sources(user_id, turns, (LoopStatus.OPEN,)):
        if lp.origin is not LoopOrigin.CONVERSATION or (only is not None and lp.id != only):
            continue
        if await service.close(lp.id, LoopStatus.BLOCKED) is not None:
            blocked += 1
    if blocked:
        log.info("outcomes.loops_blocked", user_id=user_id, count=blocked)
    return blocked
