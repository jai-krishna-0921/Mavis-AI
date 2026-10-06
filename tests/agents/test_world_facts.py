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


def test_without_web_it_is_not_offered(db, monkeypatch) -> None:
    from mavis.domain.policy import Capability
    from mavis.tools.registry import get_registry

    registry = get_registry()
    monkeypatch.setattr(registry, "available", lambda t: t.requires is not Capability.WEB)
    names = [t.name for t in simple_turn.chat_tools(1, "who is Kishor Ahuja")]
    assert "web_search" not in names


async def test_persona_rule_is_present_with_or_without_tools(db) -> None:
    from datetime import UTC, datetime

    user, _ = await users.get_or_create_by_chat(5, "Jai")
    prompt = system_prompt(user, datetime(2026, 10, 6, 4, 30, tzinfo=UTC)).lower()
    assert "real people" in prompt and "not sure" in prompt and "never invent" in prompt
    assert "biograph" in prompt
    assert "—" not in prompt and "–" not in prompt


def test_tool_rules_say_search_before_stating_world_facts() -> None:
    rules = simple_turn.TOOL_RULES
    assert "web_search" in rules and "real people" in rules and "before answering" in rules


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
    assert "real people" in system and "web_search" in system
