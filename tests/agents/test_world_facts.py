"""T4: real-world facts are grounded. Chat always offers web_search when web is available, the tool rules
say to search before stating facts about real people, companies, products, prices or news, and the persona
says to admit uncertainty rather than invent (present with or without tools)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from mavis.agents import simple_turn
from mavis.agents.persona import system_prompt
from mavis.agents.simple_turn import run_turn
from mavis.llm import models as llm
from mavis.store.repo import users
from tests.agents.test_simple_turn import msg_event

QUERIES = [
    "who is Kishor Ahuja",
    "what's the price of a Pixel 9 right now",
    "send an email to Jawahar and check my calendar for a free slot to reply to the draft",
    "remind me to forget my calendar events and list tasks",
    "is Acme Corp hiring? draft a reply to their recruiter email",
]


@pytest.mark.parametrize("query", QUERIES)
def test_chat_always_offers_web_search_when_web_is_available(db, query) -> None:
    names = [t.name for t in simple_turn.chat_tools(1, query)]
    assert "web_search" in names
    assert len(names) <= simple_turn.CHAT_TOOL_LIMIT and "web_extract" not in names


def test_without_web_configured_it_is_not_offered(db, monkeypatch) -> None:
    from mavis.config import get_settings
    from mavis.tools.integrations.wiring import tool_available
    from mavis.tools.registry import get_registry

    registry = get_registry()
    monkeypatch.setattr(registry, "available", tool_available)
    assert "web_search" in [t.name for t in simple_turn.chat_tools(1, "who is Kishor Ahuja")]
    monkeypatch.setattr(get_settings(), "web_search_enabled", False)
    assert "web_search" not in [t.name for t in simple_turn.chat_tools(1, "who is Kishor Ahuja")]


async def test_persona_rule_is_present_with_or_without_tools(db) -> None:
    from datetime import UTC, datetime

    user, _ = await users.get_or_create_by_chat(5, "Jai")
    prompt = system_prompt(user, datetime(2026, 10, 6, 4, 30, tzinfo=UTC)).lower()
    assert "real people" in prompt and "not sure" in prompt and "never invent" in prompt
    assert "search" not in prompt.split("never invent")[1][:200]  # no web promise without tools
    assert "biograph" in prompt
    assert "—" not in prompt and "–" not in prompt


def test_web_rule_is_narrow_and_says_what_the_user_told_needs_no_search() -> None:
    from mavis.agents.conversation import TOOL_RULES, WEB_RULE

    assert "web_search" not in TOOL_RULES  # the web line is added separately, only when offered
    assert "specific named real-world person" in WEB_RULE and "look something up" in WEB_RULE
    assert "the user told you" in WEB_RULE and "you remember" not in WEB_RULE
    assert "Otherwise answer normally" in WEB_RULE


async def test_web_rule_is_added_only_when_web_search_is_offered(db, channel, fake_llm, memory, bus,
                                                                 monkeypatch) -> None:
    from mavis.agents.conversation import WEB_RULE
    from mavis.config import get_settings
    from mavis.tools.integrations.wiring import tool_available
    from mavis.tools.registry import get_registry

    monkeypatch.setattr(get_registry(), "available", tool_available)
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Hi!")
    await run_turn(msg_event(user.id, "hi", "e1"))
    assert WEB_RULE in fake_llm.calls[-1][0].content
    monkeypatch.setattr(get_settings(), "web_search_enabled", False)
    fake_llm.push_text("Hi again!")
    await run_turn(msg_event(user.id, "hello", "e2"))
    system = fake_llm.calls[-1][0].content
    assert WEB_RULE not in system and "Using your tools" in system
    assert "not sure" in system  # the persona's uncertainty rule stays


@pytest.fixture
def bound(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = llm.invoke_tools

    async def spy(messages, tools, *args, **kwargs):
        seen.append([t.name for t in tools])
        return await real(messages, tools, *args, **kwargs)

    monkeypatch.setattr(llm, "invoke_tools", spy)
    return seen


@pytest.mark.parametrize("name", ["Kishor Ahuja", "Mira Castellanos", "Tomasz Wierzbicki"])
async def test_who_is_turn_searches_the_web(db, channel, fake_llm, memory, bus, bound, monkeypatch,
                                           name) -> None:
    from mavis.tools import web

    searched: list[str] = []

    async def fake_search(query: str, max_results: int = 5):
        searched.append(query)
        return [web.SearchHit(title=f"{name} profile", url="https://example.org/p", snippet="An engineer.")]

    monkeypatch.setattr(web, "search", fake_search)
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_ai(AIMessage(content="", tool_calls=[
        {"name": "web_search", "args": {"query": name}, "id": "c1"}]))
    fake_llm.push_text(f"From a quick search, {name} looks like an engineer [1].")
    await run_turn(msg_event(user.id, f"who's {name}"))
    assert "web_search" in bound[0]
    assert searched == [name]
    tool_msgs = [m for m in fake_llm.calls[1] if isinstance(m, ToolMessage)]
    assert tool_msgs and f"{name} profile" in tool_msgs[0].content
    system = fake_llm.calls[0][0].content
    assert "specific named real-world person" in system
