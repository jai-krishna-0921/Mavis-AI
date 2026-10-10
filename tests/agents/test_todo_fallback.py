"""To-dos are Mavis's own unless Google Tasks is linked; a missing account never hijacks a turn."""

from __future__ import annotations

from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage

from mavis import bus as bus_mod
from mavis.agents import simple_turn
from mavis.agents.simple_turn import run_turn
from mavis.domain import timeutil
from mavis.domain.events import Trust
from mavis.domain.integrations import ConnectionState
from mavis.domain.loops import LoopKind, LoopStatus, LoopUpsert
from mavis.domain.policy import Capability
from mavis.domain.wakeups import clean_what
from mavis.llm import models as llm
from mavis.loops.service import LoopService
from mavis.store.repo import loops as loops_repo
from mavis.store.repo import users
from mavis.store.repo import wakeups as wakeups_repo
from mavis.timers.service import WakeupService
from mavis.tools.integrations.actions import GOOGLE_CAPABILITIES
from tests.agents.test_simple_turn import msg_event

TASK_TOOLS = {"tasks_list", "tasks_add", "tasks_complete"}


def _call(name: str, args: dict, cid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


@pytest.fixture
def integ(monkeypatch, provider, cache, workspace_on):
    from mavis.tools import integrations

    def getter(value):
        def get():
            return value

        get.cache_clear = lambda: None
        return get

    monkeypatch.setattr(integrations, "get_provider", getter(provider))
    monkeypatch.setattr(integrations, "get_connection_cache", getter(cache))
    from mavis.tools.integrations import wiring
    from mavis.tools.registry import get_registry

    registry = get_registry()
    monkeypatch.setattr(registry, "capability_check", wiring.capability_check)
    simple_turn._failed_until.clear()
    return provider


@pytest.fixture
def bound(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = llm.invoke_tools

    async def spy(messages, tools, *args, **kwargs):
        seen.append([t.name for t in tools])
        return await real(messages, tools, *args, **kwargs)

    monkeypatch.setattr(llm, "invoke_tools", spy)
    return seen


async def _user(integ, linked: bool):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    for c in GOOGLE_CAPABILITIES:
        integ.set_state(user.id, c, ConnectionState.ACTIVE if linked else ConnectionState.NONE)
    return user


@pytest.mark.parametrize("text", [
    "add buy groceries and renew passport to my todo list",
    "put 'call the plumber' on my to-do list",
    "what is on my todo list?",
    "what do I need to do today",
    "done with groceries",
])
async def test_unlinked_never_sees_google_tasks_tools(db, channel, fake_llm, memory, bus, integ, bound, text):
    user = await _user(integ, linked=False)
    fake_llm.push_text("Sure.")
    await run_turn(msg_event(user.id, text))
    names = set(bound[0])
    assert not names & TASK_TOOLS
    assert {"track_loop", "pending", "complete_item", "wake_me"} <= names
    assert "connect_account" not in names
    assert integ.executed == []


async def test_linked_still_gets_google_tasks_tools(db, channel, fake_llm, memory, bus, integ, bound):
    user = await _user(integ, linked=True)
    fake_llm.push_text("Sure.")
    await run_turn(msg_event(user.id, "what is on my todo list?"))
    assert "tasks_list" in bound[0]


async def test_add_two_todos_without_google_keeps_them_and_sends_no_link(
        db, channel, fake_llm, memory, bus, integ):
    from mavis.channels.outbox_sender import OutboxSender

    user = await _user(integ, linked=False)
    fake_llm.push_ai(AIMessage(content="", tool_calls=[
        {"name": "track_loop", "args": {"kind": "COMMITMENT", "title": "Buy groceries"}, "id": "c1"},
        {"name": "track_loop", "args": {"kind": "COMMITMENT", "title": "Renew passport"}, "id": "c2"}]))
    fake_llm.push_text("Added both. Google Tasks sync is available with /connect google if you want it.")
    await run_turn(msg_event(user.id, "add buy groceries and renew passport to my todo list"))
    await OutboxSender(channel).run_once()
    assert {"Buy groceries", "Renew passport"} <= {lp.title for lp in await loops_repo.list_live(user.id)}
    assert not any("access to your Google" in t for t in channel.texts)


async def _seed(user):
    svc = LoopService(bus_mod.get_bus())
    def own(title):
        return LoopUpsert(kind=LoopKind.COMMITMENT, title=title, trust=Trust.USER)

    groceries = await svc.upsert(user.id, own("Buy groceries"))
    passport = await svc.upsert(user.id, own("Renew passport"))
    wake = await WakeupService().wake_me(
        user.id, timeutil.now() + timedelta(hours=3), "Reminder the user asked for: Call mom",
        kind="agent", reminder=True)
    return groceries, passport, wake


async def test_listing_returns_loops_and_reminders_with_refs(db, fake_llm, memory, bus, integ):
    from mavis.tools.assistant import PendingArgs, pending

    user = await _user(integ, linked=False)
    groceries, passport, wake = await _seed(user)
    out = await pending(user.id, PendingArgs())
    assert "Buy groceries" in out and "Renew passport" in out and "Call mom" in out
    assert f"loop:{groceries.id}" in out and f"reminder:{wake}" in out


async def test_completing_closes_loop_and_cancels_reminder(db, fake_llm, memory, bus, integ):
    from mavis.domain.errors import ActionFailed
    from mavis.tools.assistant import CompleteItemArgs, complete_item

    user = await _user(integ, linked=False)
    groceries, passport, wake = await _seed(user)
    await complete_item(user.id, CompleteItemArgs(ref=f"loop:{groceries.id}"))
    await complete_item(user.id, CompleteItemArgs(ref=f"reminder:{wake}"))
    assert (await loops_repo.get(groceries.id)).status is LoopStatus.DONE
    assert (await loops_repo.get(passport.id)).status is LoopStatus.OPEN
    assert await wakeups_repo.list_pending(user.id) == []
    other, _ = await users.get_or_create_by_chat(99, "Other")
    with pytest.raises(ActionFailed):
        await complete_item(other.id, CompleteItemArgs(ref=f"loop:{passport.id}"))
    with pytest.raises(ActionFailed):
        await complete_item(user.id, CompleteItemArgs(ref="groceries"))


async def test_done_with_groceries_turn_closes_it(db, channel, fake_llm, memory, bus, integ):
    user = await _user(integ, linked=False)
    groceries, _, _ = await _seed(user)
    fake_llm.push_ai(_call("pending", {}, "c1"))
    fake_llm.push_ai(_call("complete_item", {"ref": f"loop:{groceries.id}"}, "c2"))
    fake_llm.push_text("Nice, groceries are done.")
    await run_turn(msg_event(user.id, "done with groceries"))
    assert (await loops_repo.get(groceries.id)).status is LoopStatus.DONE


async def test_explicit_connect_still_offers_the_connect_tool(
        db, channel, fake_llm, memory, bus, integ, bound):
    user = await _user(integ, linked=False)
    fake_llm.push_text("Sending the link.")
    await run_turn(msg_event(user.id, "connect my google"))
    assert "connect_account" in bound[0]


async def test_capability_without_internal_equivalent_is_still_offered(
        db, channel, fake_llm, memory, bus, integ, bound):
    user = await _user(integ, linked=False)
    fake_llm.push_text("ok")
    await run_turn(msg_event(user.id, "what's on my calendar tomorrow"))
    assert any(n.startswith("calendar_") for n in bound[0]) and Capability.CALENDAR


# --- reminder and loop titles: the thing to do, in the user's voice -----------------------------------


@pytest.mark.parametrize(("raw", "name", "expected"), [
    ("Remind Test to stretch", "Test", "Stretch"),
    ("remind me to call mom", "Jai", "Call mom"),
    ("Reminder to take the chicken out", None, "Take the chicken out"),
    ("Remind Jai Krishna about the dentist", "Jai Krishna", "The dentist"),
    ("Jai should drink water", "Jai", "Drink water"),
    ("Jai to submit the report", "Jai", "Submit the report"),
    ("please remind you to stretch", "Jai", "Stretch"),
    ("  call   mom ", "Jai", "Call mom"),
    ("Renew passport", "Jai", "Renew passport"),
    ("Remind", "Jai", "Remind"),
    ("Reminders app update", "Jai", "Reminders app update"),
])
def test_clean_what(raw, name, expected):
    assert clean_what(raw, name, capitalise=True) == expected


def test_clean_what_leaves_stored_text_alone_without_capitalise():
    assert clean_what("see https://a.example/x", "Jai") == "see https://a.example/x"


async def test_wake_me_stores_the_action_not_the_instruction(db, fake_llm, memory, bus, integ):
    from mavis.tools.assistant import WakeMeArgs, wake_me

    user, _ = await users.get_or_create_by_chat(5, "Test")
    args = WakeMeArgs.model_validate({"at": (timeutil.now() + timedelta(hours=12)).replace(tzinfo=None),
                                      "reason": "Remind Test to stretch"})
    assert args.what == "Remind Test to stretch"
    await wake_me(user.id, args)
    [w] = await wakeups_repo.list_pending(user.id)
    assert w.reason == "Reminder the user asked for: Stretch"


def test_legacy_stored_title_renders_sensibly():
    from mavis.initiative.executor import reminder_text

    now = timeutil.now()
    text = reminder_text(clean_what("Remind Test to stretch", "Test"), now, now, "UTC")
    assert text == "⏰ Reminder: stretch" and "Test" not in text



async def test_complete_item_card_names_the_item_never_its_ref(db, fake_llm, memory, bus, integ):
    from mavis.tools.assistant import CompleteItemArgs, _prepare_complete
    from mavis.tools.registry import ToolContext, get_registry

    user = await _user(integ, linked=False)
    groceries, _, wake = await _seed(user)
    ctx = ToolContext(user_id=user.id)
    loop_card = await _prepare_complete(ctx, CompleteItemArgs(ref=f"loop:{groceries.id}"))
    assert loop_card.note == "Buy groceries"
    wake_card = await _prepare_complete(ctx, CompleteItemArgs(ref=f"reminder:{wake}"))
    assert wake_card.note == "Reminder: Call mom"
    assert (await _prepare_complete(ctx, CompleteItemArgs(ref="loop:999999"))).refusal
    other = await users.get_or_create_by_chat(4242, "Other")
    assert (await _prepare_complete(ToolContext(user_id=other[0].id),
                                    CompleteItemArgs(ref=f"loop:{groceries.id}"))).refusal
    preview = get_registry().get("complete_item").render_preview(CompleteItemArgs(ref=f"loop:{groceries.id}"))
    assert "loop:" not in preview
