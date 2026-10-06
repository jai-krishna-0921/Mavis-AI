"""I4: LEARN keeps strict grounding (items only from the user's own words), so the chat turn owns
agreements: when the user says yes to something Mavis suggested, or asks to remember or be reminded, the
turn itself calls track_loop or wake_me with the concrete item."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from mavis.agents import simple_turn
from mavis.agents.simple_turn import run_turn
from mavis.domain import timeutil
from mavis.domain.messages import Role
from mavis.llm import models as llm
from mavis.store.repo import messages, users
from tests.agents.test_simple_turn import msg_event


@pytest.fixture
def bound(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = llm.invoke_tools

    async def spy(msgs, tools, *args, **kwargs):
        seen.append([t.name for t in tools])
        return await real(msgs, tools, *args, **kwargs)

    monkeypatch.setattr(llm, "invoke_tools", spy)
    return seen


def test_tool_rules_make_the_turn_own_agreements() -> None:
    rules = simple_turn.TOOL_RULES
    assert "agree" in rules and "track_loop" in rules and "wake_me" in rules and "same turn" in rules


@pytest.mark.parametrize("query", ["yes", "do that", "the second one", "sure, go ahead", "ok 👍"])
def test_track_loop_and_wake_me_are_always_offered(db, query) -> None:
    names = [t.name for t in simple_turn.chat_tools(1, query)]
    assert {"track_loop", "wake_me"} <= set(names)


@pytest.mark.parametrize(("suggestion", "answer", "title"), [
    ("Want me to keep an eye on Ravi's reply about the lease?", "yes please", "Ravi's reply about the lease"),
    ("I can track two things: 1. renew the passport 2. book the vet. Which one?", "the second one",
     "Book the vet"),
    ("Should I note that you'll send Meera the deck?", "do that", "Send Meera the deck"),
])
async def test_agreement_turn_tracks_the_concrete_item(db, channel, fake_llm, memory, bus, bound,
                                                       suggestion, answer, title) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    await messages.log(user.id, Role.USER, "anything I should keep track of?")
    await messages.log(user.id, Role.ASSISTANT, suggestion)
    fake_llm.push_ai(AIMessage(content="", tool_calls=[{"name": "track_loop", "id": "c1", "args": {
        "kind": "COMMITMENT", "title": title}}]))
    fake_llm.push_text("Done, I'm tracking it.")
    await run_turn(msg_event(user.id, answer))
    assert "track_loop" in bound[0]
    from mavis.loops.service import LoopService

    titles = [lp.title for lp in await LoopService(bus).active(user.id)]
    assert title in titles
    assert timeutil.now()  # the turn ran on the pinned clock
