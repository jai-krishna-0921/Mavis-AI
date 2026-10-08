"""Track 1 T1.4 (hotfix4 H4): action claims in a chat reply are bound to what the turn did.

A reply that talks about acting while no action tool ran, or that points at an approval card that does
not exist, is re-prompted once with the tools available; the model then calls the tool or answers again.
A reply that still points at a missing card loses those sentences. Fake LLM only."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from mavis.agents import claims
from mavis.agents.conversation import run_turn
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.policy import RiskClass
from mavis.store.db import utcnow
from mavis.store.repo import approvals, outbox
from mavis.tools import assistant, chat_tools
from mavis.tools.registry import MavisTool


class BlockArgs(BaseModel):
    title: str
    start: str


class MailArgs(BaseModel):
    to: str
    body: str


def _event(user_id: int, text: str, n: int = 1) -> Event:
    return Event(id=f"tg:update:{n}", user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source="telegram", payload={"text": text}, trust=Trust.USER)


def _call(name: str, args: dict, cid: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


@pytest.fixture
def ran():
    return []


@pytest.fixture
def catalog(fresh_registry, ran):
    def recorder(name):
        async def fn(user_id, args):
            ran.append(name)
            return f"{name} done"

        return fn

    for tool in chat_tools.TOOLS:
        fresh_registry.register(tool)
    for tool in assistant.TOOLS:
        if tool.name in ("wake_me", "track_loop"):
            fresh_registry.register(replace(tool, fn=recorder(tool.name)))
        elif tool.name == "pending":
            fresh_registry.register(tool)
    fresh_registry.register(MavisTool(
        "calendar_block", "Create a calendar event or block of time.", BlockArgs, RiskClass.WRITE_SELF,
        recorder("calendar_block"), frozenset({"conversation"})))
    fresh_registry.register(MavisTool(
        "send_mail", "Send an email. The user is asked to approve first.", MailArgs, RiskClass.OUTWARD,
        recorder("send_mail"), frozenset({"conversation"}), preview=lambda a: f"Email {a.to}: {a.body}"))
    return fresh_registry


@pytest.fixture
def env(channel, fake_memory, rec_bus, catalog):
    return catalog


async def _texts(n: int = 1) -> list[str]:
    return await outbox.texts_with_dedupe_prefix(f"reply:tg:update:{n}:")


def _reprompted(call: list) -> bool:
    return any(isinstance(m, HumanMessage) and "this turn called no tool" in str(m.content) for m in call)


# --- a promise without the tool is re-prompted once --------------------------------------------------------


@pytest.mark.parametrize("said,claim,call", [
    ("Hi", "Oops, my bad. Let me create the event without the guest. Just tap Approve to confirm.",
     ("calendar_block", {"title": "Focus", "start": "2026-10-09T14:00"})),
    ("remind me at 5 to call mom", "Sure, I'll remind you at 5 to call mom.",
     ("wake_me", {"at": "2026-10-08T17:00:00", "reason": "call mom"})),
    ("block 2 to 3 pm tomorrow for deep work", "Done, your 2 to 3 pm block is on the calendar.",
     ("calendar_block", {"title": "Deep work", "start": "2026-10-09T14:00"})),
])
async def test_a_promise_without_a_tool_call_is_reprompted_and_then_acted_on(
    user, fake_llm, env, ran, said, claim, call
):
    fake_llm.push_text(claim)
    fake_llm.push_ai(_call(*call))
    fake_llm.push_text("All set.")
    await run_turn(_event(user.id, said))
    assert len(fake_llm.calls) == 3 and _reprompted(fake_llm.calls[1])
    assert ran == [call[0]]
    assert await _texts() == ["All set."]


async def test_an_outward_promise_ends_as_a_card_not_prose(user, fake_llm, env, ran):
    fake_llm.push_text("I'll email Ravi the deck now, just approve it.")
    fake_llm.push_ai(_call("send_mail", {"to": "ravi@example.com", "body": "Deck attached"}))
    fake_llm.push_text("Card's up.")
    await run_turn(_event(user.id, "email ravi the deck"))
    assert ran == []  # outward: queued, not sent
    assert [a.tool for a in await approvals.open_for_user(user.id)] == ["send_mail"]
    assert await _texts() == []  # the card is the only prompt


async def test_only_one_reprompt_and_a_missing_card_is_not_pointed_at(user, fake_llm, env, ran):
    fake_llm.push_text("I'll block 2 to 3 pm. Just tap Approve below.")
    fake_llm.push_text("Sure thing, 2 to 3 pm it is. Tap the Approve button when ready.")
    await run_turn(_event(user.id, "can you block around 2 pm?"))
    assert len(fake_llm.calls) == 2 and ran == []
    [text] = await _texts()
    assert "Approve" not in text and "Tap" not in text and text.startswith("Sure thing")


async def test_a_reprompt_that_fails_keeps_the_turn_alive(user, fake_llm, env, monkeypatch):
    from mavis.agents import conversation
    from mavis.domain.errors import LLMError

    real = conversation.react_loop
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise LLMError("down")
        return await real(*a, **k)

    monkeypatch.setattr(conversation, "react_loop", flaky)
    fake_llm.push_text("Let me set that reminder. Tap approve.")
    await run_turn(_event(user.id, "remind me to stretch at 4"))
    [text] = await _texts()
    assert "approve" not in text.lower()


# --- replies that need no second look -----------------------------------------------------------------------


@pytest.mark.parametrize("said,reply", [
    ("hey", "Hey Jai! How did the prep go?"),
    ("what time is it", "It's 9:56 AM, Tuesday."),
    ("am I free tomorrow?", "Yes, you're free from 2 to 5 pm."),
    ("I need to call mom at 5", "Want me to remind you at 5?"),
    ("who is Ada Lovelace", "She wrote the first published algorithm, for Babbage's engine."),
])
async def test_plain_answers_and_offers_are_one_call(user, fake_llm, env, said, reply):
    fake_llm.push_text(reply)
    await run_turn(_event(user.id, said))
    assert len(fake_llm.calls) == 1
    assert await _texts() == [reply]


async def test_a_turn_that_called_the_tool_is_not_reprompted(user, fake_llm, env, ran):
    fake_llm.push_ai(_call("wake_me", {"at": "2026-10-08T17:00:00", "reason": "call mom"}))
    fake_llm.push_text("Done, I'll remind you at 5 to call mom.")
    await run_turn(_event(user.id, "remind me at 5 to call mom"))
    assert len(fake_llm.calls) == 2 and ran == ["wake_me"]


async def test_pointing_at_a_card_that_is_still_waiting_is_fine(user, fake_llm, env):
    await approvals.create(user.id, None, "send_mail", {"to": "a@b.co", "body": "x"}, "Email a@b.co: x",
                           utcnow() + timedelta(hours=4))
    fake_llm.push_text("It's still waiting on the card above: tap Send when you're ready.")
    await run_turn(_event(user.id, "did that email go?"))
    assert len(fake_llm.calls) == 1
    assert await _texts() == ["It's still waiting on the card above: tap Send when you're ready."]


# --- the measure itself ------------------------------------------------------------------------------------


def _tool(name: str, description: str):
    from langchain_core.tools import StructuredTool

    async def f(text: str = "") -> str:
        return ""

    return StructuredTool.from_function(coroutine=f, name=name, description=description)


OFFERED = [
    _tool("wake_me", "Schedule a reminder at a specific future time."),
    _tool("calendar_create_event", "Create a calendar event or meeting. With guests, invites are sent."),
    _tool("mail_send", "Send an email. The user is asked to approve first."),
    _tool("mail_search", "Search emails."),
]
RISK = {"wake_me": RiskClass.WRITE_SELF, "calendar_create_event": RiskClass.WRITE_SELF,
        "mail_send": RiskClass.OUTWARD, "mail_search": RiskClass.READ}


@pytest.mark.parametrize("user_text,reply,ui,tools", [
    ("Hi", "Let me create the event without the guest.", False, ["calendar_create_event"]),
    ("ok", "Just tap Approve.", True, []),
    ("remind me to pay rent", "I'll remind you tomorrow.", False, ["wake_me"]),
    ("email ravi the deck", "Done, I've emailed Ravi the deck.", False, ["mail_send"]),
    ("hey", "Here's what's in your email today: two invoices.", False, []),  # talking about a read
    ("hey", "Hey! Good to see you.", False, []),
])
def test_check(user_text, reply, ui, tools):
    found = claims.check(reply, user_text, OFFERED, tools_called=[], card_shown=False,
                         risk_of=RISK.get)
    assert found.ui_claim is ui and found.tools == tools


def test_check_is_quiet_once_an_action_ran_or_a_card_was_shown():
    reply = "I'll create the event and email the guests; tap Approve."
    ran = claims.check("I've created the event and invited the guests.", "x", OFFERED,
                       tools_called=["calendar_create_event"], card_shown=False,
                       risk_of=RISK.get)
    carded = claims.check(reply, "x", OFFERED, tools_called=[], card_shown=True, risk_of=RISK.get)
    waiting = claims.check("It's waiting: tap Send to email it.", "did it go?", OFFERED, tools_called=[],
                           card_shown=False, waiting_tools=["mail_send"], risk_of=RISK.get)
    assert not ran.reprompt and not carded.reprompt and not waiting.reprompt


@pytest.mark.parametrize("reply,expected", [
    ("Sure thing! I'll block 2 to 3 pm. Just tap Approve to confirm.", "Sure thing! I'll block 2 to 3 pm."),
    ("Done.\nTap the button below.", "Done."),
    ("Tap approve.", claims.UI_FALLBACK),
])
def test_strip_ui_claims(reply, expected):
    assert claims.strip_ui_claims(reply) == expected
    assert "—" not in claims.UI_FALLBACK and "—" not in claims.REPROMPT
