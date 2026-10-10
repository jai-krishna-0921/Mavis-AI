"""Track 1 T1.4: tool exposure follows focus and intent, not just the new message's words.

- The tools of approvals queued, executed or failed in the last FOCUS_TURNS user turns stay offered.
- A running background task keeps the tools that manage it.
- connect_account (it messages the user by itself) only when the user asks to link an account.
- web_search with a rule that covers people, live facts, shopping and links. Fake LLM only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel

from mavis.agents import commands, conversation
from mavis.agents.conversation import run_turn
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.policy import RiskClass
from mavis.domain.tasks import ApprovalStatus, TaskKind, TaskOrigin
from mavis.llm import models as llm
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks
from mavis.tools import assistant, chat_tools
from mavis.tools.registry import MavisTool

# Catalog tools whose descriptions share no words with small talk, and more of them than the chat limit,
# so only focus can bring one in after "hi".
FILLER = [f"filler_{i}" for i in range(14)]
FOCUSABLE = ("make_block", "post_update", "file_expense")


class AnyArgs(BaseModel):
    text: str = ""


def _event(user_id: int, text: str, n: int) -> Event:
    return Event(id=f"tg:update:{n}", user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source="telegram", payload={"text": text}, trust=Trust.USER)


@pytest.fixture
def catalog(fresh_registry):
    async def fn(user_id, args):
        return "ok"

    for tool in chat_tools.TOOLS:
        fresh_registry.register(tool)
    for tool in assistant.TOOLS:
        if tool.name in ("pending", "wake_me", "track_loop", "list_tasks", "cancel_task", "remember"):
            fresh_registry.register(tool)
    for name in FILLER:
        fresh_registry.register(MavisTool(name, f"Quux zorp {name} widget.", AnyArgs, RiskClass.READ, fn,
                                          frozenset({"conversation"}), priority=90))
    for name in FOCUSABLE:
        fresh_registry.register(MavisTool(name, "Frobnicate a gizmo.", AnyArgs, RiskClass.OUTWARD, fn,
                                          frozenset({"conversation"}), priority=1))
    return fresh_registry


@pytest.fixture
def bound(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = llm.invoke_tools

    async def spy(messages, tools, *args, **kwargs):
        seen.append([t.name for t in tools])
        return await real(messages, tools, *args, **kwargs)

    monkeypatch.setattr(llm, "invoke_tools", spy)
    return seen


async def _turn(user, fake_llm, bound, text: str, n: int) -> list[str]:
    fake_llm.push_text("ok")
    await run_turn(_event(user.id, text, n))
    return bound[-1]


async def _approval(user_id: int, tool: str, status: ApprovalStatus) -> int:
    expires = utcnow() + timedelta(hours=4)
    aid = await approvals.create(user_id, None, tool, {"text": "x"}, f"{tool} x", expires)
    if status is not ApprovalStatus.PENDING:
        await approvals.set_status(aid, status, "r")
    return aid


@pytest.mark.parametrize("tool,status", [
    ("make_block", ApprovalStatus.FAILED),
    ("post_update", ApprovalStatus.EXECUTED),
    ("file_expense", ApprovalStatus.PENDING),
])
async def test_the_tool_of_a_recent_approval_stays_offered_after_small_talk(
    user, channel, fake_llm, fake_memory, rec_bus, catalog, bound, tool, status
):
    assert tool not in await _turn(user, fake_llm, bound, "please do the thing", 1)
    await _approval(user.id, tool, status)
    for n, said in enumerate(("hi", "thanks", "how's it going?"), start=2):
        assert tool in await _turn(user, fake_llm, bound, said, n), said
    # FOCUS_TURNS user turns later the conversation has moved on
    assert tool not in await _turn(user, fake_llm, bound, "what's up", 5)


async def test_a_rejected_card_does_not_hold_focus(user, channel, fake_llm, fake_memory, rec_bus, catalog,
                                                   bound):
    await _turn(user, fake_llm, bound, "do it", 1)
    await _approval(user.id, "make_block", ApprovalStatus.REJECTED)
    assert "make_block" not in await _turn(user, fake_llm, bound, "hi", 2)


async def test_a_running_task_keeps_its_management_tools(user, channel, fake_llm, fake_memory, rec_bus,
                                                          catalog, bound):
    names = await _turn(user, fake_llm, bound, "hey", 1)
    assert "cancel_task" not in names
    await tasks.create(user.id, goal="research standing desks", kind=TaskKind.TASK, origin=TaskOrigin.USER)
    names = await _turn(user, fake_llm, bound, "hey again", 2)
    assert {"cancel_task", "list_tasks"} <= set(names)


async def test_focus_never_breaks_the_turn(user, channel, fake_llm, fake_memory, rec_bus, catalog, bound,
                                           monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(approvals, "touched_since", boom)
    assert await _turn(user, fake_llm, bound, "hi", 1)


# --- connect_account only on explicit intent ---------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("connect my gmail", True),
    ("can you link my google account?", True),
    ("reconnect calendar please", True),
    ("hook up slack? connect slack", True),
    ("could you come up with some amazon links?", False),
    ("I would like to purchase a standing table from Amazon", False),
    ("what's on my calendar tomorrow", False),
    ("send me the link again", False),
    ("connect the dots for me on this plan", False),
])
def test_wants_connect(settings, text, expected):
    assert commands.wants_connect(text) is expected


@pytest.mark.parametrize("text,offered", [
    ("I'd like to buy a standing table from Amazon", False),
    ("hi", False),
    ("please connect my gmail", True),
    ("link my notion account", True),
])
async def test_connect_account_is_offered_only_when_asked(user, channel, fake_llm, fake_memory, rec_bus,
                                                          catalog, bound, text, offered):
    names = await _turn(user, fake_llm, bound, text, 1)
    assert ("connect_account" in names) is offered


# --- web tools for people, live facts, shopping and links --------------------------------------------------


def test_web_rule_covers_people_live_facts_shopping_and_links():
    rule = conversation.WEB_RULE
    for need in ("person", "live or current", "links", "where to buy", "look something up"):
        assert need in rule
    assert "never say you can't browse" in rule
    assert "—" not in rule and "–" not in rule


@pytest.mark.parametrize("text", [
    "could you come up with some amazon links for standing tables?",
    "who's kishor ahuja",
    "what's the weather in Pune right now",
])
async def test_web_search_and_its_rule_reach_the_turn(user, channel, fake_llm, fake_memory, rec_bus,
                                                      fresh_registry, bound, text):
    from mavis.tools import web

    for tool in web.TOOLS:
        fresh_registry.register(tool)
    for tool in chat_tools.TOOLS:
        fresh_registry.register(tool)
    names = await _turn(user, fake_llm, bound, text, 1)
    assert "web_search" in names and "web_extract" not in names
    assert conversation.WEB_RULE in fake_llm.calls[-1][0].content


def test_tool_query_carries_the_request_a_follow_up_continues():
    from types import SimpleNamespace

    from mavis.agents.conversation import CHAT_QUERY_TURNS, tool_query

    def m(role: str, text: str):
        return SimpleNamespace(role=role, content=text)

    history = [m("user", "what's the weather"), m("user", "send an invite to ravi for 3pm tomorrow"),
               m("assistant", "Sure, 3 to 4 PM. Any location?"), m("user", "make the location Chennai")]
    q = tool_query("make the location Chennai", "Sure, 3 to 4 PM. Any location?", history)
    assert "invite to ravi" in q and "Any location?" in q
    assert q.count("make the location Chennai") == 1  # the newest message is not repeated
    assert CHAT_QUERY_TURNS == 2 and "weather" in q
