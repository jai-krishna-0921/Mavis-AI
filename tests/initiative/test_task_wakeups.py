"""E2E run 2 (R1): a task waiting on the user gets no check-in wakeups; a running one gets one at a time."""

import pytest

from mavis.domain.decisions import InitiativeDecision, WakeupRequest
from mavis.domain.events import EventType
from mavis.domain.tasks import TaskStatus
from tests.initiative.test_wakeup_subjects import (
    agent_wakeups,
    build,
    later,
    make_task,
    signal,
)


def _ask(task_id: int, reason: str, hours: int = 6) -> InitiativeDecision:
    return InitiativeDecision(wakeups=[WakeupRequest(at=later(hours), reason=reason,
                                                     subject_kind="task", subject_id=task_id)])


@pytest.mark.parametrize("reason", ["Check on GATE research task for Bangalore", "see if the task moved",
                                    "task-check in the morning"])
async def test_no_wakeup_for_a_task_waiting_on_the_user(user, clock, recording_bus, fake_memory, reason):
    init = build(recording_bus, fake_memory)
    tid = await make_task(user, TaskStatus.AWAITING_APPROVAL)
    await init.executor.apply(user, _ask(tid, reason), signal(user, EventType.USER_QUIET, f"ev:{reason}"))
    assert await agent_wakeups(init, user) == []


async def test_a_running_task_gets_one_pending_check_at_a_time(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    tid = await make_task(user, TaskStatus.RUNNING)
    for i, reason in enumerate(["check the task", "look at the task again", "is the task done yet"]):
        await init.executor.apply(user, _ask(tid, reason, hours=6 + i),
                                  signal(user, EventType.USER_QUIET, f"ev:{i}"))
    assert len(await agent_wakeups(init, user)) == 1


async def test_checks_for_two_different_tasks_do_not_collide(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    a = await make_task(user, TaskStatus.RUNNING)
    b = await make_task(user, TaskStatus.QUEUED)
    await init.executor.apply(user, _ask(a, "check a"), signal(user, EventType.USER_QUIET, "ev:a"))
    await init.executor.apply(user, _ask(b, "check b"), signal(user, EventType.USER_QUIET, "ev:b"))
    assert len(await agent_wakeups(init, user)) == 2
