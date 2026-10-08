"""Track 1 T1.1: the approval card is the only prompt.

A chat turn whose every tool result is an approval card (queued, or an updated card shown again) sends no
prose bubble: the card, rendered by code, says what is waiting. A turn that also did something else keeps
its reply. The receipt of an approved action is the tool's own user text. Fake LLM only."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from mavis.agents.conversation import run_turn
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, JobKind, Trust
from mavis.domain.policy import RiskClass
from mavis.domain.tasks import TaskKind
from mavis.store.repo import approvals, messages, outbox, tasks
from mavis.tools import chat_tools
from mavis.tools.registry import CARD_RESULT_PREFIXES, MavisTool


class MailArgs(BaseModel):
    to: str
    body: str


class LookupArgs(BaseModel):
    name: str


def _event(user_id: int, text: str, n: int = 1) -> Event:
    return Event(id=f"tg:update:{n}", user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source="telegram", payload={"text": text}, trust=Trust.USER)


def _call(name: str, args: dict, cid: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


@pytest.fixture
def tools(fresh_registry, note_tool):
    async def mail(user_id: int, args: MailArgs) -> str:
        return "sent"

    async def lookup(user_id: int, args: LookupArgs) -> str:
        return f"{args.name}: {args.name.lower()}@example.com"

    for tool in chat_tools.TOOLS:
        fresh_registry.register(tool)
    fresh_registry.register(MavisTool(
        "send_mail", "Send an email.", MailArgs, RiskClass.OUTWARD, mail, frozenset({"conversation"}),
        preview=lambda a: f"Email {a.to}: {a.body}", identity=("to", "body"), target=("to",)))
    fresh_registry.register(MavisTool(
        "lookup_contact", "Find a contact's email address.", LookupArgs, RiskClass.READ, lookup,
        frozenset({"conversation"})))
    return note_tool


@pytest.fixture
def env(channel, fake_memory, tools):
    return tools


async def _texts(n: int = 1) -> list[str]:
    return await outbox.texts_with_dedupe_prefix(f"reply:tg:update:{n}:")


@pytest.mark.parametrize("call,said", [
    (("send_note", {"text": "running 10 min late"}), "tell Priya I'm running 10 min late"),
    (("send_mail", {"to": "ravi@example.com", "body": "Deck attached"}), "email ravi the deck"),
    (("send_note", {"text": "happy birthday!"}), "wish Arjun happy birthday"),
])
async def test_a_card_only_turn_sends_no_prose(user, fake_llm, rec_bus, env, call, said):
    fake_llm.push_ai(_call(*call))
    fake_llm.push_text("Ready when you are. Just tap Approve to send it!")
    await run_turn(_event(user.id, said))
    assert await _texts() == []
    assert [m.role for m in await messages.recent(user.id)] == ["user"]
    [card] = await approvals.open_for_user(user.id)
    assert (await tasks.get(card.task_id)).kind == TaskKind.APPROVAL  # its gate shows the card
    assert [j.kind for j in rec_bus.jobs].count(JobKind.RUN_TASK) == 1


async def test_two_cards_in_one_turn_still_send_no_prose(user, fake_llm, rec_bus, env):
    fake_llm.push_ai(AIMessage(content="", tool_calls=[
        {"name": "send_note", "args": {"text": "see you at 6"}, "id": "a"},
        {"name": "send_mail", "args": {"to": "meera@example.com", "body": "Minutes attached"}, "id": "b"},
    ]))
    fake_llm.push_text("Both are waiting for your OK.")
    await run_turn(_event(user.id, "note to Sam and mail Meera the minutes"))
    assert await _texts() == []
    assert len(await approvals.open_for_user(user.id)) == 2


async def test_a_turn_that_also_looked_something_up_keeps_its_reply(user, fake_llm, rec_bus, env):
    fake_llm.push_ai(_call("lookup_contact", {"name": "Kavya"}, "a"))
    fake_llm.push_ai(_call("send_mail", {"to": "kavya@example.com", "body": "See you Monday"}, "b"))
    fake_llm.push_text("Found Kavya's address (kavya@example.com).")
    await run_turn(_event(user.id, "email Kavya see you Monday"))
    assert await _texts() == ["Found Kavya's address (kavya@example.com)."]
    assert len(await approvals.open_for_user(user.id)) == 1


async def test_an_updated_card_shown_again_sends_no_prose(user, fake_llm, rec_bus, env):
    fake_llm.push_ai(_call("send_mail", {"to": "dev@example.com", "body": "Ship Friday"}))
    fake_llm.push_text("Waiting for your OK.")
    await run_turn(_event(user.id, "email dev that we ship Friday", 1))
    [first] = await approvals.unattached_for_user(user.id) or await approvals.open_for_user(user.id)
    await approvals.mark_prompted(first.id)
    fake_llm.push_ai(_call("send_mail", {"to": "dev@example.com", "body": "Ship Thursday"}))
    fake_llm.push_text("Updated it, take another look and approve.")
    await run_turn(_event(user.id, "make it Thursday", 2))
    result = [m for m in fake_llm.calls[-1] if isinstance(m, ToolMessage)][-1]
    assert result.content.startswith(CARD_RESULT_PREFIXES)
    assert await _texts(2) == []


async def test_an_action_already_waiting_keeps_the_reply(user, fake_llm, rec_bus, env):
    """No new card is shown, so the reply is the only thing pointing the user to the earlier one."""
    fake_llm.push_ai(_call("send_note", {"text": "lunch at 1?"}))
    fake_llm.push_text("ok")
    await run_turn(_event(user.id, "ask Leo lunch at 1?", 1))
    fake_llm.push_ai(_call("send_note", {"text": "lunch at 1?"}))
    fake_llm.push_text("That one is still waiting on the card above.")
    await run_turn(_event(user.id, "did you send the note to Leo?", 2))
    assert await _texts(2) == ["That one is still waiting on the card above."]


async def test_the_queued_result_tells_the_model_the_card_is_the_prompt(user, fake_llm, rec_bus, env):
    fake_llm.push_ai(_call("send_note", {"text": "hi"}))
    fake_llm.push_text("x")
    await run_turn(_event(user.id, "say hi to Ana"))
    result = [m for m in fake_llm.calls[-1] if isinstance(m, ToolMessage)][-1]
    assert "card" in result.content and "Tell the user it is ready" not in result.content


@pytest.mark.parametrize("goal,expected", [
    ("Provide specs and budget options for standing tables.",
     "On it: provide specs and budget options for standing tables. I'll send it over when it's ready."),
    ("NASA launch schedule for October", "On it: NASA launch schedule for October. I'll send it over when "
     "it's ready."),
    ("  compare   Goa hotels!  ", "On it: compare Goa hotels. I'll send it over when it's ready."),
])
def test_task_ack_is_rendered_from_the_goal(goal, expected):
    assert chat_tools.task_ack(goal) == expected


def test_task_ack_clips_a_long_goal_at_a_word():
    ack = chat_tools.task_ack("research " + "very " * 60 + "long things")
    assert len(ack) < 200 and "#" not in ack and " ver." not in ack
