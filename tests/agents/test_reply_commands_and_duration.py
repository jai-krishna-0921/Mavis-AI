"""Track 1 T1.4 (hotfix4 H6): command names in replies match the real commands, and an event with only
a start time gets the configured default length (used, mentioned, changeable), never a question."""

from __future__ import annotations

from datetime import datetime

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from mavis.agents import commands, conversation
from mavis.agents.conversation import run_turn
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.policy import RiskClass
from mavis.store.repo import outbox
from mavis.tools import chat_tools
from mavis.tools.integrations.actions import CalendarCreateArgs
from mavis.tools.registry import MavisTool


def _event(user_id: int, text: str, n: int = 1) -> Event:
    return Event(id=f"tg:update:{n}", user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source="telegram", payload={"text": text}, trust=Trust.USER)


# --- slash commands --------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Send /connect_google to link it.", "Send /connect to link it."),  # no Google consent when off
    ("Try /connectgmail.", "Try /connect gmail."),
    ("Use /link calendar.", "Use /connect calendar."),
    ("/connection shows what's linked", "/connections shows what's linked"),
    ("Just send /conect gmail", "Just send /connect gmail"),
    ("Send /connect again in a minute.", "Send /connect again in a minute."),
    ("/disconnect Gmail removes it", "/disconnect gmail removes it"),
    ("/start over", "/start over"),
])
def test_commands_in_replies_match_the_real_ones(settings, raw, expected):
    assert commands.canonical_commands(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Send /connect_google to link it.", "Send /connect google to link it."),
    ("Try /connectgmail or /connect calendar.", "Try /connect google or /connect google."),
    ("/link drive", "/connect google"),
    ("/disconnect gmail-legacy", "/disconnect gmail-legacy"),  # a real alias of /disconnect
])
def test_with_workspace_every_google_service_is_one_google_command(workspace_on, raw, expected):
    assert commands.canonical_commands(raw) == expected


@pytest.mark.parametrize("raw", [
    "and/or",
    "see https://example.com/connect/callback",
    "files live in /home/jk/notes",
    "ratio 3/4 and 50/50",
    "email me@x.com/connect",
])
def test_slashes_that_are_not_commands_are_left_alone(settings, raw):
    assert commands.canonical_commands(raw) == raw


def test_every_rewritten_command_parses_as_a_real_command(settings):
    for raw in ("/connect_slack", "/connectnotion", "/link", "/connection", "/disconect gmail"):
        name, _ = commands.parse_command(commands.canonical_commands(raw))
        assert name in commands.KNOWN_COMMANDS, raw


async def test_a_chat_reply_carries_the_real_command(user, channel, fake_llm, fake_memory, rec_bus,
                                                     fresh_registry):
    fake_llm.push_text("You can link it with /connect_gmail whenever you like.")
    await run_turn(_event(user.id, "how do I add my email?"))
    assert await outbox.texts_with_dedupe_prefix("reply:tg:update:1:") == [
        "You can link it with /connect gmail whenever you like."]


# --- default event length --------------------------------------------------------------------------------


@pytest.mark.parametrize("minutes", [30, 45, 60, 90])
def test_only_a_start_gets_the_configured_default_length(settings, monkeypatch, minutes):
    monkeypatch.setattr(settings, "default_event_minutes", minutes)
    args = CalendarCreateArgs(summary="Deep work", start=datetime(2026, 10, 9, 14, 0))
    assert args.duration_minutes == minutes
    assert CalendarCreateArgs(summary="x", start=datetime(2026, 10, 9, 14), duration_minutes=15
                              ).duration_minutes == 15  # what they said always wins


def test_the_default_length_is_an_hour_out_of_the_box(settings):
    assert settings.default_event_minutes == 60


class CreateArgs(BaseModel):
    summary: str


@pytest.fixture
def calendar_catalog(fresh_registry):
    async def create(user_id, args):
        return "created"

    for tool in chat_tools.TOOLS:
        fresh_registry.register(tool)
    fresh_registry.register(MavisTool("calendar_create_event", "Create a calendar event or meeting.",
                                      CreateArgs, RiskClass.WRITE_SELF, create, frozenset({"conversation"})))
    return fresh_registry


@pytest.mark.parametrize("text", ["block 2 pm tomorrow", "add a meeting at 4", "hi"])
async def test_the_duration_rule_reaches_the_model_with_the_configured_length(
    user, channel, fake_llm, fake_memory, rec_bus, calendar_catalog, settings, monkeypatch, text
):
    monkeypatch.setattr(settings, "default_event_minutes", 45)
    fake_llm.push_text("ok")
    await run_turn(_event(user.id, text))
    system = fake_llm.calls[0][0].content
    assert conversation.DURATION_RULE.format(minutes=45) in system
    assert "don't ask how long" in system


async def test_no_duration_rule_without_an_event_tool(user, channel, fake_llm, fake_memory, rec_bus,
                                                      fresh_registry):
    for tool in chat_tools.TOOLS:
        fresh_registry.register(tool)
    fake_llm.push_ai(AIMessage(content="ok"))
    await run_turn(_event(user.id, "block 2 pm tomorrow"))
    assert "don't ask how long" not in fake_llm.calls[0][0].content
