"""Every cancel path goes through cancellation.cancel_by_user; the loop stops before its next model call."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mavis.agents import cancellation
from mavis.agents.cancellation import TaskCancelled
from mavis.agents.react import react_loop
from mavis.agents.task_buttons import handle_task_button
from mavis.channels import progress_card
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.progress import CardFinal
from mavis.domain.tasks import TaskStatus
from mavis.store.db import utcnow
from mavis.store.repo import tasks, users
from tests.machine.fakes import RecordingCards


@pytest.fixture(autouse=True)
def _clean():
    cancellation.reset_for_tests()
    yield
    cancellation.reset_for_tests()


def _tap(user_id: int, data: str) -> Event:
    return Event(id=f"tg:update:{data}:{user_id}", user_id=user_id, type=EventType.BUTTON_PRESSED,
                 occurred_at=utcnow(), source="telegram", payload={"data": data}, trust=Trust.USER)


async def _running(user_id: int, goal: str) -> int:
    tid = await tasks.create(user_id, goal=goal)
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.RUNNING)
    return tid


async def test_cancel_runs_hooks_and_sets_the_flag(db, user):
    seen = []

    async def hook(task_id):
        seen.append(task_id)

    cancellation.register_cancel_hook(hook)
    tid = await _running(user.id, "scrape four bakery menus")
    assert await cancellation.cancel_by_user(user.id, tid) is True
    assert await cancellation.is_cancelled(tid) and seen == [tid]
    assert (await tasks.get(tid)).status == TaskStatus.CANCELLED


async def test_a_failing_hook_does_not_stop_the_others(db, user):
    seen = []

    async def bad(task_id):
        raise RuntimeError("x")

    async def good(task_id):
        seen.append(task_id)

    cancellation.register_cancel_hook(bad)
    cancellation.register_cancel_hook(good)
    tid = await _running(user.id, "convert a recipe")
    await cancellation.cancel_by_user(user.id, tid)
    assert seen == [tid]


async def test_cancel_button_finalizes_the_card(db, user):
    rec = RecordingCards()
    progress_card.set_cards(rec)
    tid = await _running(user.id, "rank five podcasts")
    await handle_task_button(_tap(user.id, f"tk:{tid}:x"), f"tk:{tid}:x")
    assert ("final", tid, CardFinal.CANCELLED) in rec.calls
    assert (await tasks.get(tid)).status == TaskStatus.CANCELLED


@pytest.mark.parametrize("data", ["tk:abc:x", "tk::x", "tk:12", "tk:12:y"])
async def test_malformed_task_buttons_do_nothing(db, user, data):
    rec = RecordingCards()
    progress_card.set_cards(rec)
    await handle_task_button(_tap(user.id, data), data)
    assert rec.calls == []


async def test_cancel_tap_from_another_user_is_rejected(db, user):
    other, _ = await users.get_or_create_by_chat(70_707, "Mallory")
    tid = await _running(user.id, "find a plumber")
    await handle_task_button(_tap(other.id, f"tk:{tid}:x"), f"tk:{tid}:x")
    assert (await tasks.get(tid)).status == TaskStatus.RUNNING
    assert not await cancellation.is_cancelled(tid)


async def test_cancel_stops_the_loop_before_the_next_model_call(fake_llm):
    stop = {"now": False}
    from langchain_core.tools import tool

    @tool
    def ping(x: str) -> str:
        """echo"""
        stop["now"] = True
        return x

    fake_llm.push_ai(AIMessage(content="", tool_calls=[{"name": "ping", "args": {"x": "a"}, "id": "c1"}]))

    async def should_stop():
        return stop["now"]

    with pytest.raises(TaskCancelled):
        await react_loop([ping], [HumanMessage("go")], max_steps=5, should_stop=should_stop)
    assert not fake_llm.ai_queue  # exactly one model call happened


async def test_no_should_stop_means_unchanged_behaviour(fake_llm):
    fake_llm.push_text("answer")
    result = await react_loop([], [HumanMessage("hi")], max_steps=2)
    assert result.text == "answer"


async def test_cancel_task_tool_uses_the_same_path(db, user):
    from mavis.tools.assistant import CancelTaskArgs, cancel_task

    rec = RecordingCards()
    progress_card.set_cards(rec)
    tid = await _running(user.id, "tidy my reading list")
    await cancel_task(user.id, CancelTaskArgs(task_id=tid))
    assert await cancellation.is_cancelled(tid)
    assert ("final", tid, CardFinal.CANCELLED) in rec.calls


async def test_spawned_worker_loop_also_stops(db, user, fake_llm, monkeypatch):
    """Every task loop (specialist or spawned worker) polls the same flag."""
    from mavis.agents import spawn
    from mavis.tools.registry import current_task_id

    tid = await _running(user.id, "collect four library opening hours")
    await cancellation.cancel_by_user(user.id, tid)
    fake_llm.push_text("should never be asked")
    token = current_task_id.set(tid)
    try:
        with pytest.raises(TaskCancelled):
            await spawn.spawn_agent(user.id, "researcher", "look it up", [])
    finally:
        current_task_id.reset(token)
    assert fake_llm.ai_queue  # no model call happened
