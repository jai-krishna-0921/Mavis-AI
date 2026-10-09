"""Run / resume orchestrator tasks as durable LangGraph threads (thread_id = task:{id}).

Status changes after the graph starts all go through `tasks.claim`, so a cancel that landed while
the graph ran is never overwritten. One planned task runs at a time (`task_max_concurrency`); a
finished task kicks the next queued one. APPROVAL tasks are exempt from the limit.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from langgraph.types import Command

from mavis import bus
from mavis.agents import cancellation, checkpointing, interrupts
from mavis.agents.orchestrator_graph import (
    EXPIRED_LEAD,
    PARTIAL_LEAD,
    build_orchestrator,
    card_wanted,
    complete_task,
    initial_state,
    partial_messages,
    publish_completed,
)
from mavis.agents.task_clock import TaskClock, current_clock
from mavis.agents.task_dispatch import enqueue_run
from mavis.channels.formatting import verbatim
from mavis.config import get_settings
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.plans import Plan
from mavis.domain.progress import CardFinal
from mavis.domain.tasks import TaskKind, TaskStatus
from mavis.llm.tracing import callbacks
from mavis.policy import approvals as approval_flow
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks

log = structlog.get_logger()

_LIVE = (TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL)


_STALE_MARGIN_S = 60
_user_locks: dict[int, asyncio.Lock] = {}


def clock_s(task: Any) -> float:
    """The task's own wall clock: `plan["clock_s"]` when the planner extended it, else task_timeout_s."""
    try:
        return float((task.plan or {}).get("clock_s") or get_settings().task_timeout_s)
    except (TypeError, ValueError, AttributeError):
        return get_settings().task_timeout_s


def _still_within_clock(task: Any, now: datetime) -> bool:
    """A RUNNING task past the default cutoff whose extended clock has not run out yet."""
    started = task.started_at
    return started is not None and started > now - timedelta(seconds=clock_s(task) + _STALE_MARGIN_S)


async def _reap_stale(user_id: int) -> None:
    """A task RUNNING past its wall-clock limit plus a margin lost its worker: fail it so it stops
    counting toward the concurrency limit. The query uses the shortest clock; each candidate is then
    checked against its own (an extended machine clock is not stale)."""
    now = utcnow()
    cutoff = now - timedelta(seconds=get_settings().task_timeout_s + _STALE_MARGIN_S)
    for stale in await tasks.stale_running(user_id, cutoff):
        if _still_within_clock(stale, now):
            continue
        log.warning("task.stale_running", task_id=stale.id)
        await _fail(stale.id, user_id, "that task stalled and was stopped")


async def recover_tasks(user_id: int | None = None, *, restarted_at: datetime | None = None) -> int:
    """Self-heal the task queue so a lost worker can never block a user's tasks. Safe to run repeatedly.

    1. A RUNNING task whose run began more than the wall-clock limit plus a margin ago lost its worker.
       With `restarted_at` (worker start), every RUNNING task that began before it lost its worker too:
       there is one worker, and a run never outlives its process. Each is failed through `tasks.claim`,
       which tells the user and closes its approvals.
    2. Every user with QUEUED tasks gets its next one re-enqueued when a slot is free (a deferred task
       has no job of its own; a duplicate RUN_TASK is harmless, run_task claims QUEUED once).
    Returns the number of tasks failed."""
    now = utcnow()
    cutoff = now - timedelta(seconds=get_settings().task_timeout_s + _STALE_MARGIN_S)
    if restarted_at is not None:
        cutoff = max(cutoff, restarted_at)
    failed = 0
    for stale in await tasks.running_started_before(cutoff, user_id):
        if restarted_at is None and _still_within_clock(stale, now):
            continue  # its clock was extended and is still running
        log.warning("task.recovered_stuck_running", task_id=stale.id, restart=restarted_at is not None)
        reason = ("I was restarted partway through it" if restarted_at is not None
                  else "that task stalled and was stopped")
        await _fail(stale.id, stale.user_id, reason)
        failed += 1
    failed += await _expire_waiting(user_id, now)
    for uid in await tasks.users_with_queued(user_id):
        await _kick_next_queued(uid)
    return failed


async def _expire_waiting(user_id: int | None, now: datetime) -> int:
    """A task waiting on the user (a connection or an approval) longer than `task_await_ttl_s` ends: what it
    gathered is delivered as PARTIAL, or it fails with a plain reason when it gathered nothing. Returns the
    number ended."""
    cutoff = now - timedelta(seconds=get_settings().task_await_ttl_s)
    ended = 0
    for waiting in await tasks.awaiting_started_before(cutoff, user_id):
        log.warning("task.await_expired", task_id=waiting.id)
        if await _end_unfinished(waiting.id, waiting.user_id, "it waited too long on a connection or your OK",
                                 lead=EXPIRED_LEAD):
            ended += 1
    return ended


async def _saved_state(task_id: int) -> dict | None:
    """The graph's last checkpointed state (finished step outputs included), or None."""
    try:
        async with checkpointing.open_checkpointer() as saver:
            graph = build_orchestrator().compile(checkpointer=saver)
            snap = await graph.aget_state({"configurable": {"thread_id": f"task:{task_id}"}})
        return dict(snap.values) if snap is not None and snap.values else None
    except Exception:  # noqa: BLE001 - salvage is best effort; the task must still reach a final state
        log.warning("task.saved_state_unreadable", task_id=task_id)
        return None


async def _end_unfinished(task_id: int, user_id: int, reason: str, *, lead: str = PARTIAL_LEAD) -> bool:
    """End a task that cannot go on, without losing finished work: PARTIAL with the finished steps' output
    (a finished summary step is the answer) or, with nothing to show, FAILED with `reason`.
    True when this call ended it."""
    row = await tasks.get(task_id)
    if row is None or row.status not in (TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL):
        return False
    state = await _saved_state(task_id)
    messages = partial_messages(state, lead) if state else []
    if not messages:
        await _fail(task_id, user_id, reason)
        return True
    try:
        ended = await complete_task(state, messages, TaskStatus.PARTIAL,
                                    "it ran out of time, so this covers only what I finished")
    except Exception:  # noqa: BLE001 - never let delivery trouble leave the task without a final state
        log.exception("task.salvage_failed", task_id=task_id)
        after = await tasks.get(task_id)
        if after is not None and after.status in _LIVE:
            await _fail(task_id, user_id, reason)  # the claim never happened: fail it plainly
            return True
        if after is None or after.status != TaskStatus.PARTIAL:
            return False  # cancelled or finished meanwhile: not ours to report
        # Our claim went through and the delivery broke: _fail would no-op on a terminal task. Deliver the
        # outcome again (the event id dedupes) or, failing that, say it directly, and close the approvals.
        await _redeliver(task_id, user_id, state, messages)
        await _close_approvals(task_id, user_id)
        return True
    if ended:
        await _close_approvals(task_id, user_id)
    return ended


async def _redeliver(task_id: int, user_id: int, state: dict, messages: list[str]) -> None:
    try:
        await publish_completed(task_id, user_id, messages, list(dict.fromkeys(state.get("artifacts", []))),
                                TaskStatus.PARTIAL)
    except Exception:  # noqa: BLE001
        log.exception("task.redelivery_failed", task_id=task_id)
        with contextlib.suppress(Exception):
            await approval_flow.say(user_id, "\n\n".join(messages), dedupe_key=f"task:{task_id}:completed")


async def recover_tasks_on_start() -> None:
    """Worker start hook (registered before the approval sweep, so kicked tasks can take answers)."""
    await recover_tasks(restarted_at=utcnow())


async def run_task(task_id: int) -> None:
    task = await tasks.get(task_id)
    if task is None or task.status != TaskStatus.QUEUED:
        return
    # The per-user lock makes check-then-claim atomic for concurrent RUN_TASK jobs in this process.
    async with _user_locks.setdefault(task.user_id, asyncio.Lock()):
        await _reap_stale(task.user_id)
        # An APPROVAL task only shows a prompt and waits (no LLM work before its interrupt), so it never
        # waits for the task slot: a chat "send this email" prompt appears even while research runs.
        if (task.kind != TaskKind.APPROVAL
                and await tasks.running_count(task.user_id) >= get_settings().task_max_concurrency):
            log.info("task.deferred_concurrency", task_id=task_id)
            return
        if not await tasks.claim(task_id, TaskStatus.QUEUED, TaskStatus.RUNNING):
            return
    await _drive(task_id, task.user_id, initial_state(task))


async def resume_task(task_id: int, resume_value: dict) -> None:
    task = await tasks.get(task_id)
    if task is None or not await tasks.claim(task_id, TaskStatus.AWAITING_APPROVAL, TaskStatus.RUNNING):
        return
    await _drive(task_id, task.user_id, Command(resume=resume_value))


async def _drive(task_id: int, user_id: int, graph_input: Any) -> None:
    s = get_settings()
    task_row = await tasks.get(task_id)
    if task_row is not None and card_wanted(task_row):
        progress = asyncio.create_task(_card_after(task_id, user_id, s.progress_card_after_s))
    else:
        progress = asyncio.create_task(_progress_after(task_id, user_id, s.task_progress_after_s))
    result: dict | None = None
    limit = asyncio.timeout(s.task_timeout_s)
    try:
        async with limit, checkpointing.open_checkpointer() as saver:
            clock = TaskClock(limit, s.task_timeout_s, s.task_timeout_max_s,
                              asyncio.get_running_loop().time())
            if task_row is not None and clock_s(task_row) > s.task_timeout_s:
                clock.extend_to(clock_s(task_row))  # a resumed machine task keeps the clock its plan got
            clock_token = current_clock.set(clock)
            try:
                graph = build_orchestrator().compile(checkpointer=saver)
                config = {
                    "configurable": {"thread_id": f"task:{task_id}"},
                    "callbacks": callbacks(), "recursion_limit": 80, "run_name": f"task:{task_id}",
                }
                result = await graph.ainvoke(graph_input, config=config)
            finally:
                current_clock.reset(clock_token)
    except TimeoutError:
        if limit.expired():
            await _end_unfinished(task_id, user_id, "that took longer than I allow for one task")
        else:  # a timeout inside a tool or HTTP call, not the task wall clock
            log.exception("task.inner_timeout", task_id=task_id)
            await _fail(task_id, user_id, "something I depend on timed out")
    except Exception:  # noqa: BLE001 - the task row must always reach a final state
        log.exception("task.crashed", task_id=task_id)
        await _end_unfinished(task_id, user_id, "something broke on my side")
    finally:
        progress.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress

    try:
        if result is not None:
            pending = result.get("__interrupt__") or []
            if pending:
                # AWAITING_APPROVAL covers every "waiting for the user" pause (approval or connect).
                # Claimed from RUNNING, so a cancel that landed meanwhile stands and nothing is prompted.
                if await tasks.claim(task_id, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL):
                    await _cards().tool_called(task_id, "waiting for your OK")
                    if not await interrupts.dispatch_interrupt(task_id, user_id, pending[0].value):
                        log.error("task.unhandled_interrupt", task_id=task_id, payload=pending[0].value)
        await _report_executed_after_stop(task_id, user_id)
    except Exception:  # noqa: BLE001 - delivery trouble must not block the queue
        log.exception("task.post_run_failed", task_id=task_id)
    cancellation.forget(task_id)  # this run is over: the in-process flag has done its job
    await _kick_next_queued(user_id)


async def _fail(task_id: int, user_id: int, reason: str) -> None:
    if not await tasks.claim(task_id, _LIVE, TaskStatus.FAILED, error=reason):
        return  # cancelled (or finished) meanwhile: say nothing about a task the user stopped
    from mavis.initiative import task_delivery  # lazy: delivery imports the ping policy

    # Files are the work: whatever the task made goes out even when it fails, and the line names them.
    await task_delivery.deliver_pending_artifacts(user_id, task_id)
    names = [Path(a.path).name for a in await tasks.artifacts_for(task_id) if a.delivered_at is not None]
    sent_line = f" I'd already sent you {_join_names(names)}." if names else ""
    await _cards().finalize(task_id, CardFinal.FAILED)
    await approval_flow.say(user_id, f"Hit a snag on that task: {reason}.{sent_line} Want me to try again?",
                            dedupe_key=f"task:{task_id}:failed")
    await _close_approvals(task_id, user_id)


def _join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


fail_task = _fail  # public name for the approval sweep


async def _close_approvals(task_id: int, user_id: int) -> None:
    """A failed task never resumes, so its approvals must not stay tappable or stuck."""
    await approvals.reject_open_for_task(task_id)
    if await approvals.fail_unstarted_for_task(task_id, "the task failed before this ran"):
        from mavis.policy import outcomes  # lazy: policy imports the registry

        for ap in await approvals.failed_for_task(task_id):  # never ran: its loops are not live either
            await outcomes.block_loops_of_failed_approval(ap.id)
    for ap in await approvals.may_have_run_for_task(task_id):
        await approval_flow.say(
            user_id,
            "One thing to check: this may have gone through before the task failed.\n"
            + verbatim(ap.preview),
            dedupe_key=f"approval:{ap.id}:may_have_run",
        )


async def _report_executed_after_stop(task_id: int, user_id: int) -> None:
    """Approve-then-cancel (or a failure after approving): the action did run, so say so.

    A finished task reports it through its responder; a cancelled or failed one never gets there.
    """
    task = await tasks.get(task_id)
    if task is None or task.status not in (TaskStatus.CANCELLED, TaskStatus.FAILED):
        return
    if task.result_text is not None:
        return  # it finished through its responder (FAILED from its outcomes), which reported what ran
    for ap in await approvals.executed_for_task(task_id):
        await approval_flow.say(
            user_id,
            "Heads up: this went through before the task stopped.\n"
            + verbatim(ap.preview),
            dedupe_key=f"approval:{ap.id}:ran_after_stop",
        )


def _cards():
    from mavis.channels.progress_card import hook_cards  # lazy: channels import the bus

    return hook_cards()


async def _card_after(task_id: int, user_id: int, delay_s: float) -> None:
    """Send the card once the plan exists and the task has run `delay_s` (machine plans: at once)."""
    from mavis.agents.orchestrator_graph import plan_is_machine

    waited = 0.0
    while True:
        task = await tasks.get(task_id)
        if task is None or task.status not in _LIVE:
            return
        if task.plan:
            plan = Plan.model_validate(task.plan)
            if _cards().has_card(task_id):
                return  # the planner started it already (machine plan or no delay)
            if waited >= delay_s or plan_is_machine(plan):
                await _cards().start(task_id, user_id, task.goal, plan.steps, tainted=bool(task.tainted))
                return
        pause = min(1.0, max(0.05, delay_s - waited))
        await asyncio.sleep(pause)
        waited += pause


async def _progress_after(task_id: int, user_id: int, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    task = await tasks.get(task_id)
    # an APPROVAL task's slow Edit is about an email, not a long job: no "still on it" line
    if task is None or task.status != TaskStatus.RUNNING or task.kind == TaskKind.APPROVAL:
        return
    await bus.get_bus().publish(Event(
        id=f"task:{task_id}:progress", user_id=user_id, type=EventType.TASK_PROGRESS,
        occurred_at=utcnow(), source="agent", trust=Trust.SYSTEM,
        payload={"task_id": task_id, "goal": task.goal, "origin": task.origin},
    ))


async def _kick_next_queued(user_id: int) -> None:
    for ap_task in await tasks.queued_approval_tasks(user_id):  # never slot-bound (left over from before)
        await enqueue_run(ap_task.id, user_id)
    nxt = await tasks.next_queued(user_id)
    if nxt is not None and await tasks.running_count(user_id) < get_settings().task_max_concurrency:
        await enqueue_run(nxt.id, user_id)
