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


# --- partial_messages: finished work is delivered, never silently lost ------------------------------------


def _state(deps: dict[str, list[str]], texts: dict[str, str]) -> dict:
    plan = Plan(goal="g", steps=[PlanStep(id=i, agent="research", instruction=i, depends_on=d)
                                 for i, d in deps.items()])
    return {"plan": plan.model_dump(), "results": {k: {"ok": True, "text": v} for k, v in texts.items()}}


@pytest.mark.parametrize(("deps", "texts", "expect_in", "expect_out"), [
    # independent steps: the last one does not fold the others in, so all are delivered
    ({"s1": [], "s2": [], "s3": []}, {"s1": "ONE", "s2": "TWO", "s3": "THREE"}, ["ONE", "TWO", "THREE"], []),
    # a chain: the closing step folds in the earlier ones, it is the answer alone
    ({"s1": [], "s2": ["s1"], "s3": ["s2"]}, {"s1": "ONE", "s2": "TWO", "s3": "THREE"}, ["THREE"],
     ["ONE", "TWO"]),
    # mixed: s3 builds on s2 only, so s1 would be lost if s3 stood alone
    ({"s1": [], "s2": [], "s3": ["s2"]}, {"s1": "ONE", "s2": "TWO", "s3": "THREE"}, ["ONE", "TWO", "THREE"],
     []),
    # a diamond: transitive dependencies count
    ({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}, {"a": "AA", "b": "BB", "c": "CC", "d": "DD"},
     ["DD"], ["AA", "BB", "CC"]),
    # an unfinished middle step is not owed: the closing step covers every finished one
    ({"s1": [], "s2": ["s1"], "s3": ["s1"]}, {"s1": "ONE", "s3": "THREE"}, ["THREE"], ["ONE"]),
    # the closing step did not finish: everything that did is delivered in plan order
    ({"s1": [], "s2": [], "s3": ["s1", "s2"]}, {"s1": "ONE", "s2": "TWO"}, ["ONE", "TWO"], []),
])
def test_partial_delivery_follows_the_dependencies(deps, texts, expect_in, expect_out):
    text = "\n".join(og.partial_messages(_state(deps, texts), lead=""))
    assert all(t in text for t in expect_in)
    assert not any(t in text for t in expect_out)
    if len(expect_in) > 1:
        assert [text.index(t) for t in expect_in] == sorted(text.index(t) for t in expect_in)  # plan order


def test_more_than_three_bubbles_say_that_the_rest_was_cut():
    big = "x" * 3000
    deps = {f"s{i}": [] for i in range(1, 6)}
    msgs = og.partial_messages(_state(deps, {k: big for k in deps}))
    assert len(msgs) == 3 and msgs[0] == og.PARTIAL_LEAD  # the lead plus two body bubbles
    assert msgs[-1].endswith(og.CUT_NOTE)
    assert not any(c in m for m in msgs for c in "—–")
    short = og.partial_messages(_state({"s1": [], "s2": []}, {"s1": "ONE", "s2": "TWO"}), lead="")
    assert not any(og.CUT_NOTE in m for m in short)  # nothing was cut, nothing is said


def test_bubbles_cap_keeps_the_limit_and_names_the_cut():
    text = "\n\n".join("y" * 3000 for _ in range(7))
    out = og._bubbles(text)
    assert len(out) == 3 and out[-1].endswith(og.CUT_NOTE)
    assert og._bubbles("short") == ["short"]


# --- a delivery that breaks after the terminal claim still reaches the user and closes approvals ----------


async def _salvage_with_broken_publish(user, fake_llm, rec_bus, monkeypatch, fails):
    from datetime import timedelta

    from mavis.domain import timeutil
    from mavis.store.repo import approvals

    monkeypatch.setattr(og, "run_step_agent", _step_returning({"s1": "raw", "s2": "FINAL ANSWER"}))

    async def _boom(state):
        raise RuntimeError("critic down")

    monkeypatch.setattr(og, "critic", _boom)
    real = rec_bus.publish
    left = {"n": fails}

    async def flaky(event):
        if left["n"] > 0:
            left["n"] -= 1
            raise ConnectionError("bus down")
        return await real(event)

    monkeypatch.setattr(rec_bus, "publish", flaky)
    fake_llm.push_structured(TWO)
    tid = await tasks.create(user.id, goal="g")
    aid = await approvals.create(user.id, tid, "send_note", {"text": "x"}, "Send note: x",
                                 timeutil.now() + timedelta(hours=48))
    await orchestrator.run_task(tid)
    return tid, aid


@pytest.mark.parametrize("fails", [1, 99])
async def test_delivery_failing_after_the_claim_still_tells_the_user_and_closes_approvals(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch, fails
):
    from mavis.store.repo import approvals

    tid, aid = await _salvage_with_broken_publish(user, fake_llm, rec_bus, monkeypatch, fails)
    assert (await tasks.get(tid)).status == TaskStatus.PARTIAL
    events = "".join("".join(e.payload["messages"]) for e in _delivered(rec_bus))
    told = "".join(m.text for m in sent) + events
    assert "FINAL ANSWER" in told
    assert (await approvals.get(aid)).status != "pending"
