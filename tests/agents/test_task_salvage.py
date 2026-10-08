"""E2E run 2 (R2): a task never loses finished work. Planner/LLM errors are retried then degrade; the wall
clock delivers finished step outputs; nothing escapes the runner as an unhandled exception."""

from __future__ import annotations

import asyncio

import pytest

from mavis.agents import orchestrator
from mavis.agents import orchestrator_graph as og
from mavis.config import get_settings
from mavis.domain.decisions import ComposedMessage
from mavis.domain.errors import LLMError
from mavis.domain.events import EventType
from mavis.domain.plans import CriticVerdict, Plan, PlanStep
from mavis.domain.tasks import StepOutcome, TaskStatus
from mavis.store.repo import tasks

TWO = Plan(goal="g", steps=[
    PlanStep(id="s1", agent="research", instruction="find options"),
    PlanStep(id="s2", agent="research", instruction="write summary", depends_on=["s1"]),
])
ONE = Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="find options")])


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(og, "LLM_RETRY_DELAYS_S", (0.0, 0.0))


def _step_returning(text_by_id: dict[str, str]):
    async def _step(step, user_id, context):
        return StepOutcome(ok=True, text=text_by_id[step.id])

    return _step


def _delivered(rec_bus):
    return [e for e in rec_bus.events if e.type == EventType.TASK_COMPLETED]


async def test_planner_deadline_is_retried_then_the_plan_is_used(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(og, "run_step_agent", _step_returning({"s1": "found 3 desks"}))
    fake_llm.push_error(LLMError("LLM deadline exceeded"), structured=True)
    fake_llm.push_structured(ONE)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Here are 3 desks."]))
    tid = await tasks.create(user.id, goal="desks")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.DONE


async def test_planner_down_degrades_to_one_research_step(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(og, "run_step_agent", _step_returning({"s1": "found 3 desks"}))
    for _ in range(3):  # first try and both retries
        fake_llm.push_error(LLMError("LLM deadline exceeded"), structured=True)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Here are 3 desks."]))
    tid = await tasks.create(user.id, goal="desks")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.DONE


async def test_responder_down_still_delivers_the_gathered_results(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(og, "run_step_agent", _step_returning({"s1": "found 3 desks: A, B, C"}))
    fake_llm.push_structured(ONE)
    for _ in range(3):
        fake_llm.push_error(LLMError("LLM deadline exceeded"), structured=True)
    tid = await tasks.create(user.id, goal="desks")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.DONE
    assert "A, B, C" in "".join(_delivered(rec_bus)[-1].payload["messages"])


async def test_critic_down_accepts_what_was_gathered(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(og, "run_step_agent", _step_returning({"s1": "x", "s2": "summary text"}))
    fake_llm.push_structured(TWO)
    for _ in range(3):
        fake_llm.push_error(LLMError("LLM deadline exceeded"), structured=True)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Summary."]))
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.DONE


async def test_wall_clock_delivers_a_finished_summary(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    """Run 2 task 21: every step incl. the summary finished, then the clock ran out in review."""
    monkeypatch.setattr(get_settings(), "task_timeout_s", 1.5)
    steps = {"s1": "raw notes", "s2": "SUMMARY: desk A wins"}
    monkeypatch.setattr(og, "run_step_agent", _step_returning(steps))

    async def _slow_critic(state):
        await asyncio.sleep(10)
        return {"todo": []}

    monkeypatch.setattr(og, "critic", _slow_critic)
    fake_llm.push_structured(TWO)
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    task = await tasks.get(tid)
    assert task.status == TaskStatus.PARTIAL
    assert "desk A wins" in (task.result_text or "")
    [event] = _delivered(rec_bus)
    assert event.payload["status"] == "partial" and "desk A wins" in "".join(event.payload["messages"])
    assert "raw notes" not in task.result_text  # the closing step already folds the earlier ones in


async def test_wall_clock_without_a_summary_delivers_each_finished_step(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(get_settings(), "task_timeout_s", 1.5)
    three = Plan(goal="g", steps=[
        PlanStep(id="s1", agent="research", instruction="a"),
        PlanStep(id="s2", agent="research", instruction="b", depends_on=["s1"]),
        PlanStep(id="s3", agent="research", instruction="c", depends_on=["s2"]),
    ])

    async def _step(step, user_id, context):
        if step.id == "s3":
            await asyncio.sleep(10)
        return StepOutcome(ok=True, text=f"output of {step.id}")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(three)
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    task = await tasks.get(tid)
    assert task.status == TaskStatus.PARTIAL
    assert "output of s1" in task.result_text and "output of s2" in task.result_text
    assert "output of s3" not in task.result_text


async def test_wall_clock_with_nothing_finished_still_fails_plainly(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(get_settings(), "task_timeout_s", 1.0)

    async def _slow(step, user_id, context):
        await asyncio.sleep(10)

    monkeypatch.setattr(og, "run_step_agent", _slow)
    fake_llm.push_structured(ONE)
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.FAILED
    assert "longer than" in sent[-1].text


async def test_crash_after_work_finished_delivers_it_and_logs_no_exception(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(og, "run_step_agent", _step_returning({"s1": "raw", "s2": "FINAL ANSWER"}))

    async def _boom(state):
        raise RuntimeError("secret-token-9")

    monkeypatch.setattr(og, "critic", _boom)
    fake_llm.push_structured(TWO)
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    task = await tasks.get(tid)
    assert task.status == TaskStatus.PARTIAL and "FINAL ANSWER" in task.result_text
    assert "secret-token-9" not in task.result_text + (task.error or "")


def test_verdict_model_still_importable() -> None:
    assert CriticVerdict(accept=True).accept
