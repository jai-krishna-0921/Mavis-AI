"""The runner and the registry feed the card; labels are code-made; no card for fast or initiative tasks."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from mavis.agents import orchestrator
from mavis.agents import orchestrator_graph as og
from mavis.channels import progress_card
from mavis.config import get_settings
from mavis.domain.decisions import ComposedMessage
from mavis.domain.plans import CriticVerdict, Plan, PlanStep
from mavis.domain.policy import RiskClass
from mavis.domain.progress import CardFinal, StepState
from mavis.domain.tasks import StepOutcome, TaskKind, TaskOrigin, TaskStatus
from mavis.store.repo import tasks
from mavis.tools.registry import MavisTool, current_task_id, default_label, host_of
from tests.machine.fakes import RecordingCards


@pytest.fixture
def cards():
    rec = RecordingCards()
    progress_card.set_cards(rec)
    return rec


def _plan(n: int = 2) -> Plan:
    return Plan(goal="g", steps=[PlanStep(id=f"s{i}", agent="research", instruction="x", title=f"Step {i}")
                                 for i in range(1, n + 1)])


class Args(BaseModel):
    url: str = ""


@pytest.mark.parametrize("name,label", [("web_search", "web search"), ("files_list", "files list"),
                                        ("machine_run_python", "machine run python")])
def test_default_label(name, label):
    assert default_label(name) == label


@pytest.mark.parametrize("url,host", [("https://www.Shop.example/p?q=1", "shop.example"),
                                      ("http://docs.example.org/a", "docs.example.org"), ("not a url", "")])
def test_host_of(url, host):
    assert host_of(url) == host


async def test_registry_reports_a_code_made_label(db, user, fresh_registry, cards):
    async def fn(user_id, args):
        return "SECRET PAGE TEXT that must never reach the card"

    fresh_registry.register(MavisTool("peek", "d", Args, RiskClass.READ, fn, frozenset({"research"}),
                                      progress_label=lambda a, out: f"opened {host_of(a.url)}"))
    token = current_task_id.set(77)
    try:
        await fresh_registry.invoke(fresh_registry.get("peek"), user.id, Args(url="https://news.example/x"))
    finally:
        current_task_id.reset(token)
    assert ("tool", 77, "opened news.example") in cards.calls
    assert not any("SECRET" in str(c) for c in cards.calls)


async def test_registry_default_label_and_no_task_means_no_call(db, user, fresh_registry, cards):
    async def fn(user_id, args):
        return "x"

    fresh_registry.register(MavisTool("tidy_up", "d", Args, RiskClass.READ, fn, frozenset({"research"})))
    await fresh_registry.invoke(fresh_registry.get("tidy_up"), user.id, Args())
    assert cards.calls == []  # chat turn: no task id
    token = current_task_id.set(5)
    try:
        await fresh_registry.invoke(fresh_registry.get("tidy_up"), user.id, Args())
    finally:
        current_task_id.reset(token)
    assert cards.calls == [("tool", 5, "tidy up")]


@pytest.mark.parametrize("outcome,state", [
    (StepOutcome(ok=True, text="a"), StepState.DONE),
    (StepOutcome(ok=True, text="a", partial=True), StepState.PARTIAL),
    (StepOutcome(ok=False, error="boom"), StepState.FAILED),
])
def test_step_state_of(outcome, state):
    assert og.step_state_of(outcome) is state


async def test_user_task_gets_a_card_with_step_hooks_and_final(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch, cards
):
    monkeypatch.setattr(get_settings(), "progress_card_after_s", 0.0)

    async def _step(step, user_id, context):
        await asyncio.sleep(0.05)
        return StepOutcome(ok=True, text=f"did {step.id}")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_plan(2))
    fake_llm.push_structured(CriticVerdict(accept=True))  # two steps: the critic reviews
    fake_llm.push_structured(ComposedMessage(send=True, messages=["all done"]))
    tid = await tasks.create(user.id, goal="collect three bus timetables")
    await orchestrator.run_task(tid)
    kinds = cards.kinds(tid)
    assert kinds[0] == "start" and kinds[-1] == "final"
    assert kinds.count("step_started") == 2 and kinds.count("step_finished") == 2
    assert cards.calls[-1] == ("final", tid, CardFinal.DONE)
    assert not [m for m in sent if "Still on it" in m.text]


async def test_no_card_for_fast_or_initiative_tasks(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch, cards
):
    async def _step(step, user_id, context):
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _step)
    for origin in (TaskOrigin.USER, TaskOrigin.INITIATIVE):
        fake_llm.push_structured(_plan(1))
        fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
        tid = await tasks.create(user.id, goal=f"quick {origin.value} thing", origin=origin)
        await orchestrator.run_task(tid)
        assert "start" not in cards.kinds(tid)


async def test_failed_task_finalizes_couldnt_finish(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch, cards
):
    monkeypatch.setattr(get_settings(), "progress_card_after_s", 0.0)
    monkeypatch.setattr(get_settings(), "task_timeout_s", 0.3)

    async def _slow(step, user_id, context):
        await asyncio.sleep(2)
        return StepOutcome(ok=True, text="late")

    monkeypatch.setattr(og, "run_step_agent", _slow)
    fake_llm.push_structured(_plan(1))
    tid = await tasks.create(user.id, goal="a slow lookup of ferry fares")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.FAILED
    assert cards.calls[-1] == ("final", tid, CardFinal.FAILED)


async def test_cards_off_keeps_the_fixed_progress_line(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch, cards
):
    from mavis.domain.events import EventType

    monkeypatch.setattr(get_settings(), "progress_card_enabled", False)
    monkeypatch.setattr(get_settings(), "task_progress_after_s", 0.01)

    async def _step(step, user_id, context):
        await asyncio.sleep(0.1)
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_plan(1))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    tid = await tasks.create(user.id, goal="draft a packing list")
    await orchestrator.run_task(tid)
    assert cards.calls == []
    assert any(e.type == EventType.TASK_PROGRESS for e in rec_bus.events)


def test_card_wanted_rules(settings):
    class T:
        def __init__(self, origin, kind):
            self.origin, self.kind = origin, kind

    assert orchestrator.card_wanted(T(TaskOrigin.USER.value, TaskKind.TASK.value))
    assert not orchestrator.card_wanted(T(TaskOrigin.INITIATIVE.value, TaskKind.TASK.value))
    assert not orchestrator.card_wanted(T(TaskOrigin.USER.value, TaskKind.APPROVAL.value))
