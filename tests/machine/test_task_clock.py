from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from mavis.agents import orchestrator
from mavis.agents.task_clock import TaskClock
from mavis.config import get_settings
from mavis.domain.tasks import TaskStatus
from mavis.store.db import Session, utcnow
from mavis.store.models import Task
from mavis.store.repo import tasks


@pytest.mark.parametrize("ask,expect", [(100, 120), (900, 900), (5000, 1200)])
async def test_extend_is_clamped_between_base_and_max(ask, expect):
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(120) as t:  # noqa: ASYNC100 - only the deadline arithmetic is tested
        clock = TaskClock(t, base_s=120, max_s=1200, started=loop.time())
        assert clock.extend_to(ask) == expect
        assert abs(t.when() - (clock.started + expect)) < 0.01


async def test_extension_lets_a_long_step_finish():
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(0.1) as t:
        TaskClock(t, base_s=0.1, max_s=1.0, started=loop.time()).extend_to(0.5)
        await asyncio.sleep(0.2)  # would have timed out at 0.1 s


async def test_stale_cutoff_respects_the_extended_clock(db, user, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_timeout_s", 480)
    long_id = await tasks.create(user.id, goal="analyse a big spreadsheet")
    short_id = await tasks.create(user.id, goal="look up a word")
    started = utcnow() - timedelta(seconds=700)
    async with Session() as s:
        for tid, plan in ((long_id, {"goal": "g", "steps": [], "clock_s": 900}), (short_id, None)):
            row = await s.get(Task, tid)
            row.status, row.started_at, row.plan = TaskStatus.RUNNING.value, started, plan
        await s.commit()
    await orchestrator._reap_stale(user.id)
    assert (await tasks.get(long_id)).status == TaskStatus.RUNNING
    assert (await tasks.get(short_id)).status == TaskStatus.FAILED


@pytest.fixture
def machine_specialist(monkeypatch):
    from mavis.agents.specialists import SPECIALISTS
    from mavis.agents.specialists.base import Specialist

    monkeypatch.setitem(SPECIALISTS, "cruncher", Specialist(name="cruncher", description="d", prompt="p",
                                                            machine=True))


@pytest.mark.parametrize("agent,extended", [("cruncher", True), ("research", False)])
async def test_planner_extends_the_clock_only_for_machine_plans(db, user, fake_llm, machine_specialist,
                                                                monkeypatch, agent, extended):
    from mavis.agents import orchestrator_graph as og
    from mavis.agents.task_clock import current_clock
    from mavis.domain.plans import Plan, PlanStep

    monkeypatch.setattr(get_settings(), "machine_task_timeout_s", 700)
    tid = await tasks.create(user.id, goal="chart my monthly water bills")
    fake_llm.push_structured(Plan(goal="g", steps=[PlanStep(id="s1", agent=agent, instruction="x")]))
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(480) as t:
        clock = TaskClock(t, base_s=480, max_s=1200, started=loop.time())
        token = current_clock.set(clock)
        try:
            await og.planner(og.initial_state(await tasks.get(tid)))
        finally:
            current_clock.reset(token)
    saved = (await tasks.get(tid)).plan
    assert (saved.get("clock_s") == 700) is extended and clock.total_s == (700 if extended else 480)
    assert orchestrator.clock_s(await tasks.get(tid)) == (700 if extended else 480)


async def test_a_resumed_run_keeps_the_extended_clock(db, user, monkeypatch):
    """A resume (after an approval) builds a fresh clock: it starts from the plan's saved clock_s."""
    seen = []

    class Graph:
        async def ainvoke(self, graph_input, config):
            seen.append(orchestrator.current_clock.get().total_s)
            return {}

    class Builder:
        def compile(self, checkpointer):
            return Graph()

    monkeypatch.setattr(orchestrator, "build_orchestrator", lambda: Builder())
    monkeypatch.setattr(get_settings(), "task_timeout_s", 480)
    for clock_total in (900, 1100):
        tid = await tasks.create(user.id, goal=f"rebuild a {clock_total} row report")
        await tasks.save_plan(tid, {"goal": "g", "steps": [], "clock_s": clock_total})
        await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.RUNNING)
        await orchestrator._drive(tid, user.id, {})
    assert seen == [900, 1100]
