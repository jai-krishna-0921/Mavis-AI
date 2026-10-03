"""A5: context never instructs the model to assert absence; absence is checked with tools, an empty search
means "not found with that query", and context items carry their source id so they can be read."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from mavis.agents.conversation import TOOL_RULES
from mavis.attention.digest import GUIDE, render_digest
from mavis.tools.integrations.mail_render import render_search

T = datetime(2026, 10, 3, 6, 30, tzinfo=UTC)
# Wording that tells the model to claim something is absent or that nothing is new.
DENIAL = re.compile(r"\bsay (?:that )?(?:nothing|there is nothing|there's nothing|no )|nothing new needs",
                    re.IGNORECASE)


@dataclass
class Row:
    id: int
    message_id: str
    verdict: str
    summary: str
    source: str = "mail"
    action: str = ""
    feedback: str | None = None
    received_at: datetime = T


ROW_SETS = [
    [],
    [Row(1, "m-routine", "log", "account update from meetup: terms of service")],
    [Row(1, "m-a", "notify", "security from examplebank: new login"),
     Row(2, "m-b", "log", "receipt from exampleshop: order shipped")],
    [Row(1, "drive:f1", "brief", "share from drive: Q3 plan", source="drive"),
     Row(2, "abc123", "ask", "money movement from bank: debit", action="confirm")],
    [Row(i, f"id{i}", "dropped", f"newsletter {i}") for i in range(5)],
]


@pytest.mark.parametrize("rows", ROW_SETS)
def test_digest_never_instructs_denial(rows):
    out = render_digest(rows, [], 0, "Asia/Kolkata", 24)
    assert not DENIAL.search(out)
    assert "search" in GUIDE.lower()


def test_empty_digest_is_a_neutral_fact():
    out = render_digest([Row(1, "m1", "log", "receipt from shop: shipped")], [], 0, "UTC", 24)
    assert "No new emails were classified as needing them in this window." in out
    assert "(nothing notable)" not in out


@pytest.mark.parametrize("rows", ROW_SETS[1:])
def test_digest_lines_carry_the_message_id_of_mail_rows(rows):
    shown = [r for r in rows if r.verdict in ("ask", "notify", "brief", "forwarded")]
    out = render_digest(rows, shown, 0, "UTC", 24)
    for r in shown:
        if r.source == "mail":
            assert f"message_id={r.message_id}" in out
        else:
            assert f"message_id={r.message_id}" not in out  # not a Gmail id: mail_read cannot use it


def test_chat_prompt_requires_verifying_absence():
    rules = TOOL_RULES.lower()
    assert "before saying" in rules and "doesn't exist" in rules
    assert "not found with that query" in rules
    assert "broader" in rules and ("sender" in rules or "domain" in rules)
    assert "message_id" in rules


@pytest.mark.parametrize("empty", [{"messages": []}, {}, {"data": {"messages": []}}, None])
@pytest.mark.parametrize("query", ["Meetup TOS", "from:bank.example after:2026/10/01", ""])
def test_empty_search_is_a_neutral_fact_naming_the_query(empty, query):
    """Inside the untrusted tool output only a fact; the guidance lives in the system prompt rules."""
    from types import SimpleNamespace

    out = render_search(empty, args=SimpleNamespace(query=query))
    assert out == (f"No emails matched the query {query!r}." if query else "No emails matched the query.")
    assert not DENIAL.search(out) and "try" not in out.lower()


# --- a scripted turn: the first search misses, a broader one finds the email listed in context -------


async def test_turn_retries_broader_and_reads_the_listed_email(db, channel, fake_llm, memory, bus, integ,
                                                               monkeypatch):
    from mavis.agents import context_hooks
    from mavis.agents.simple_turn import run_turn
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.domain.integrations import ConnectionState, ToolResult
    from mavis.domain.policy import Capability
    from mavis.store.repo import users
    from tests.agents.test_simple_turn import msg_event
    from tests.agents.test_simple_turn_tools import READ_DATA, SEARCH_DATA, _call, _tool_messages

    user, _ = await users.get_or_create_by_chat(77, "Jai")
    integ.set_state(user.id, Capability.GMAIL, ConnectionState.ACTIVE)
    listed = Row(9, "m1", "notify", "event from meetup: AI Builders Meetup this Thursday")

    async def digest(user_id, text):
        return render_digest([listed], [listed], 0, "UTC", 24)

    context_hooks.register_context_provider(digest)
    queries: list[str] = []

    async def execute(user_ref, action, args):
        integ.executed.append((user_ref.user_id, action, args))
        if action == "mail.search":
            queries.append(args["query"])
            hit = "meetup" in args["query"].lower() and "builders talk" not in args["query"].lower()
            return ToolResult(ok=True, data=SEARCH_DATA if hit else {"messages": []})
        return ToolResult(ok=True, data=READ_DATA)

    monkeypatch.setattr(integ, "execute", execute)
    fake_llm.push_ai(_call("mail_search", {"query": "builders talk invite"}, "c1"))
    fake_llm.push_ai(_call("mail_search", {"query": "from:meetup.com"}, "c2"))
    fake_llm.push_ai(_call("mail_read", {"message_id": "m1"}, "c3"))
    fake_llm.push_text("Found it: AI Builders Meetup, Thursday 6pm. RSVP by Wednesday noon.")
    try:
        await run_turn(msg_event(user.id, "what was that builders talk email you mentioned?"))
    finally:
        context_hooks.clear_context_providers()

    first_prompt = fake_llm.calls[0][0].content
    assert "message_id=m1" in str(fake_llm.calls[0])  # the listed item can be read directly
    assert "not found with that query" in first_prompt.lower()
    assert not DENIAL.search(str(fake_llm.calls[0]))
    [miss] = _tool_messages(fake_llm.calls[1])
    assert "No emails matched the query 'builders talk invite'." in miss.content
    assert queries == ["builders talk invite", "from:meetup.com"]  # retried within the step budget
    await OutboxSender(channel).run_once()
    assert channel.texts and "RSVP" in channel.texts[-1]


@pytest.fixture
def integ(monkeypatch, provider, cache):
    from mavis.agents import simple_turn
    from mavis.tools import integrations
    from tests.agents.test_simple_turn_tools import _getter

    monkeypatch.setattr(integrations, "get_provider", _getter(provider))
    monkeypatch.setattr(integrations, "get_connection_cache", _getter(cache))
    simple_turn._failed_until.clear()
    return provider
