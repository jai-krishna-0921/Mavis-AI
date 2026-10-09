"""E2E run 2 (R1): a task waiting on the user (connection or approval) is not "running". It must not absorb
new requests, must not be described as running, and expires after a TTL to PARTIAL with what it gathered."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from mavis.agents import orchestrator
from mavis.config import get_settings
from mavis.domain.decisions import TaskRequest
from mavis.domain.events import EventType, JobKind
from mavis.domain.tasks import TaskKind, TaskOrigin, TaskStatus
from mavis.store.db import Session, utcnow
from mavis.store.models import Task
from mavis.store.repo import tasks
from mavis.tools import chat_tools

GOAL = "start a background research task on GATE coaching in Bangalore"


async def _task_in(user_id: int, status: TaskStatus, goal: str = GOAL) -> int:
    tid = await tasks.create(user_id, goal=goal)
    await tasks.set_status(tid, status)
    return tid


@pytest.mark.parametrize("status", [TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE, TaskStatus.PARTIAL,
                                    TaskStatus.FAILED, TaskStatus.CANCELLED])
async def test_only_queued_or_running_tasks_absorb_a_duplicate(user, status) -> None:
    await _task_in(user.id, status)
    assert await tasks.find_active_duplicate(user.id, GOAL) is None


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
async def test_queued_and_running_tasks_still_dedupe(user, status) -> None:
    tid = await _task_in(user.id, status)
    found = await tasks.find_active_duplicate(user.id, GOAL)
    assert found is not None and found.id == tid


async def test_new_request_is_not_absorbed_by_a_task_waiting_on_a_connection(user, rec_bus) -> None:
    stalled = await _task_in(user.id, TaskStatus.AWAITING_APPROVAL)
    out = (await chat_tools.start_task(user.id, chat_tools.StartTaskArgs(goal=GOAL))).for_model()
    assert "already" not in out.lower() and "running" not in out.lower()
    started = [j.payload["task_id"] for j in rec_bus.jobs if j.kind == JobKind.RUN_TASK]
    assert len(started) == 1 and started[0] != stalled


async def test_reply_for_a_queued_duplicate_says_queued_not_running(user, rec_bus) -> None:
    await _task_in(user.id, TaskStatus.QUEUED)
    result = await chat_tools.start_task(user.id, chat_tools.StartTaskArgs(goal=GOAL))
    assert "queued" in result.user_text.lower() or "next" in result.user_text.lower()
    assert "running" not in result.user_text.lower()


async def test_reply_for_a_running_duplicate_says_running(user, rec_bus) -> None:
    await _task_in(user.id, TaskStatus.RUNNING)
    result = await chat_tools.start_task(user.id, chat_tools.StartTaskArgs(goal=GOAL))
    assert "working on" in result.user_text.lower() or "running" in result.user_text.lower()


# --- TTL ------------------------------------------------------------------------------------------


async def _age(task_id: int, hours: float) -> None:
    async with Session() as s:
        await s.execute(update(Task).where(Task.id == task_id)
                        .values(started_at=utcnow() - timedelta(hours=hours)))
        await s.commit()


async def test_awaiting_task_expires_after_the_ttl_with_nothing_gathered(user, rec_bus, sent) -> None:
    tid = await _task_in(user.id, TaskStatus.AWAITING_APPROVAL)
    await _age(tid, 25)
    await orchestrator.recover_tasks(user.id)
    task = await tasks.get(tid)
    assert task.status == TaskStatus.FAILED
    assert "waited" in (task.error or "").lower()
    assert any("couldn't" in m.text.lower() or "snag" in m.text.lower() for m in sent)


async def test_awaiting_task_inside_the_ttl_is_left_alone(user, rec_bus, sent) -> None:
    tid = await _task_in(user.id, TaskStatus.AWAITING_APPROVAL)
    await _age(tid, 2)
    await orchestrator.recover_tasks(user.id)
    assert (await tasks.get(tid)).status == TaskStatus.AWAITING_APPROVAL
    assert sent == []


async def test_ttl_is_configurable(user, rec_bus, sent, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "task_await_ttl_s", 3600)
    tid = await _task_in(user.id, TaskStatus.AWAITING_APPROVAL)
    await _age(tid, 2)
    await orchestrator.recover_tasks(user.id)
    assert (await tasks.get(tid)).status == TaskStatus.FAILED


async def test_approval_kind_tasks_are_not_expired_by_the_ttl(user, rec_bus, sent) -> None:
    tid = await tasks.create(user.id, goal="send", kind=TaskKind.APPROVAL)
    await tasks.set_status(tid, TaskStatus.AWAITING_APPROVAL)
    await _age(tid, 30)
    await orchestrator.recover_tasks(user.id)
    assert (await tasks.get(tid)).status == TaskStatus.AWAITING_APPROVAL


async def test_expired_awaiting_task_delivers_what_it_gathered_as_partial(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
) -> None:
    from mavis.agents import orchestrator_graph as og
    from mavis.domain.errors import ConnectionRequired
    from mavis.domain.plans import Plan, PlanStep
    from mavis.domain.policy import Capability
    from mavis.domain.tasks import StepOutcome

    async def _step(step, user_id, context):
        if step.id == "s2":
            raise ConnectionRequired(Capability.NOTION, "needs notion")
        return StepOutcome(ok=True, text="Coaching centres: Alpha, Beta, Gamma")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(Plan(goal="g", steps=[
        PlanStep(id="s1", agent="research", instruction="find centres"),
        PlanStep(id="s2", agent="research", instruction="save to notion"),
    ]))
    tid = await tasks.create(user.id, goal="research coaching, save to notion")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.AWAITING_APPROVAL
    await _age(tid, 25)
    await orchestrator.recover_tasks(user.id)
    task = await tasks.get(tid)
    assert task.status == TaskStatus.PARTIAL
    assert "Alpha" in (task.result_text or "")
    done = [e for e in rec_bus.events if e.type == EventType.TASK_COMPLETED]
    assert done and done[-1].payload["status"] == "partial"
    assert "Alpha" in "".join(done[-1].payload["messages"])


def test_origin_enum_unchanged() -> None:
    assert TaskOrigin.USER.value == "user" and TaskRequest(goal="x").goal == "x"
