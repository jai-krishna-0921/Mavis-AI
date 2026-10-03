"""Run / resume orchestrator tasks as durable LangGraph threads (thread_id = task:{id}).

Status changes after the graph starts all go through `tasks.claim`, so a cancel that landed while
the graph ran is never overwritten. One task runs at a time (`task_max_concurrency`); a finished
task kicks the next queued one.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

import structlog
from langgraph.types import Command

from mavis import bus
from mavis.agents import checkpointing, interrupts
from mavis.agents.orchestrator_graph import build_orchestrator, initial_state
from mavis.agents.task_dispatch import enqueue_run
from mavis.channels.formatting import sanitize_line
from mavis.config import get_settings
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.tasks import TaskStatus
from mavis.llm.tracing import callbacks
from mavis.policy import approvals as approval_flow
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks

log = structlog.get_logger()

_LIVE = (TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL)


_STALE_MARGIN_S = 60
_user_locks: dict[int, asyncio.Lock] = {}


async def _reap_stale(user_id: int) -> None:
    """A task RUNNING past the wall-clock limit plus a margin lost its worker: fail it so it stops
    counting toward the concurrency limit."""
    cutoff = utcnow() - timedelta(seconds=get_settings().task_timeout_s + _STALE_MARGIN_S)
    for stale in await tasks.stale_running(user_id, cutoff):
        log.warning("task.stale_running", task_id=stale.id)
        await _fail(stale.id, user_id, "that task stalled and was stopped")


async def run_task(task_id: int) -> None:
    task = await tasks.get(task_id)
    if task is None or task.status != TaskStatus.QUEUED:
        return
    # The per-user lock makes check-then-claim atomic for concurrent RUN_TASK jobs in this process.
    async with _user_locks.setdefault(task.user_id, asyncio.Lock()):
        await _reap_stale(task.user_id)
        if await tasks.running_count(task.user_id) >= get_settings().task_max_concurrency:
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
    progress = asyncio.create_task(_progress_after(task_id, user_id, s.task_progress_after_s))
    result: dict | None = None
    limit = asyncio.timeout(s.task_timeout_s)
    try:
        async with limit, checkpointing.open_checkpointer() as saver:
            graph = build_orchestrator().compile(checkpointer=saver)
            config = {
                "configurable": {"thread_id": f"task:{task_id}"},
                "callbacks": callbacks(), "recursion_limit": 80, "run_name": f"task:{task_id}",
            }
            result = await graph.ainvoke(graph_input, config=config)
    except TimeoutError:
        if limit.expired():
            await _fail(task_id, user_id, "that took longer than I allow for one task")
        else:  # a timeout inside a tool or HTTP call, not the task wall clock
            log.exception("task.inner_timeout", task_id=task_id)
            await _fail(task_id, user_id, "something I depend on timed out")
    except Exception:  # noqa: BLE001 - the task row must always reach a final state
        log.exception("task.crashed", task_id=task_id)
        await _fail(task_id, user_id, "something broke on my side")
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
                    if not await interrupts.dispatch_interrupt(task_id, user_id, pending[0].value):
                        log.error("task.unhandled_interrupt", task_id=task_id, payload=pending[0].value)
        await _report_executed_after_stop(task_id, user_id)
    except Exception:  # noqa: BLE001 - delivery trouble must not block the queue
        log.exception("task.post_run_failed", task_id=task_id)
    await _kick_next_queued(user_id)


async def _fail(task_id: int, user_id: int, reason: str) -> None:
    if not await tasks.claim(task_id, _LIVE, TaskStatus.FAILED, error=reason):
        return  # cancelled (or finished) meanwhile: say nothing about a task the user stopped
    await approval_flow.say(user_id, f"Hit a snag on that task: {reason}. Want me to try again?",
                            dedupe_key=f"task:{task_id}:failed")
    await _close_approvals(task_id, user_id)


fail_task = _fail  # public name for the approval sweep


async def _close_approvals(task_id: int, user_id: int) -> None:
    """A failed task never resumes, so its approvals must not stay tappable or stuck."""
    await approvals.reject_open_for_task(task_id)
    await approvals.fail_unstarted_for_task(task_id, "the task failed before this ran")
    for ap in await approvals.may_have_run_for_task(task_id):
        await approval_flow.say(
            user_id,
            "One thing to check: this may have gone through before the task failed.\n"
            + "\n".join(sanitize_line(line) for line in ap.preview.splitlines()),
            dedupe_key=f"approval:{ap.id}:may_have_run",
        )


async def _report_executed_after_stop(task_id: int, user_id: int) -> None:
    """Approve-then-cancel (or a failure after approving): the action did run, so say so.

    A finished task reports it through its responder; a cancelled or failed one never gets there.
    """
    task = await tasks.get(task_id)
    if task is None or task.status not in (TaskStatus.CANCELLED, TaskStatus.FAILED):
        return
    for ap in await approvals.executed_for_task(task_id):
        await approval_flow.say(
            user_id,
            "Heads up: this went through before the task stopped.\n"
            + "\n".join(sanitize_line(line) for line in ap.preview.splitlines()),
            dedupe_key=f"approval:{ap.id}:ran_after_stop",
        )


async def _progress_after(task_id: int, user_id: int, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    task = await tasks.get(task_id)
    if task is None or task.status != TaskStatus.RUNNING:
        return
    await bus.get_bus().publish(Event(
        id=f"task:{task_id}:progress", user_id=user_id, type=EventType.TASK_PROGRESS,
        occurred_at=utcnow(), source="agent", trust=Trust.SYSTEM,
        payload={"task_id": task_id, "goal": task.goal, "origin": task.origin},
    ))


async def _kick_next_queued(user_id: int) -> None:
    nxt = await tasks.next_queued(user_id)
    if nxt is not None and await tasks.running_count(user_id) < get_settings().task_max_concurrency:
        await enqueue_run(nxt.id, user_id)
