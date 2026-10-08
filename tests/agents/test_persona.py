from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from mavis.agents.persona import recent_messages, should_ask_name, split_bubbles, system_prompt
from mavis.store.repo import users

NOW = datetime(2026, 10, 2, 4, 30, tzinfo=UTC)  # 10:00 in Asia/Kolkata, a Friday


async def test_system_prompt_has_identity_time_and_rules(db) -> None:
    user, _ = await users.get_or_create_by_chat(1, "Jai")
    prompt = system_prompt(user, NOW, context="## Known facts\n- Jawahar is a friend")
    assert "You are Mavis" in prompt
    assert "Friday 02 October 2026, 10:00" in prompt and "Asia/Kolkata" in prompt
    assert "Jai" in prompt
    assert "behind the curtain" in prompt
    assert "waits for their OK" in prompt
    assert "never claim to be human" in prompt.lower()
    assert prompt.rstrip().endswith("- Jawahar is a friend")


async def test_prompt_style_and_capabilities(db) -> None:
    user, _ = await users.get_or_create_by_chat(4, "Jai")
    prompt = system_prompt(user, NOW)
    assert "—" not in prompt and "–" not in prompt
    assert "Never use dashes as punctuation" in prompt and "line containing just ---" in prompt
    assert "No headings, no tables" in prompt
    assert "Working today:" in prompt and "coming soon" in prompt
    for item in ("remember", "Gmail", "/connect", "/connections", "/disconnect", "morning check-in",
                 "follow up after them", "decks"):
        assert item in prompt
    assert prompt.index("morning check-in") < prompt.index("coming soon")
    # QA: the old block made the model quote its own instructions back at the user
    assert "exactly" not in prompt
    assert "never claim any of these works yet" not in prompt
    assert 'Coming soon (say "soon"' not in prompt
    assert "tech support" not in prompt.lower()


async def test_system_prompt_uses_configured_agent_name(db, monkeypatch) -> None:
    from mavis.config import get_settings

    monkeypatch.setenv("AGENT_NAME", "Nova")
    get_settings.cache_clear()
    user, _ = await users.get_or_create_by_chat(3, "Jai")
    prompt = system_prompt(user, NOW)
    assert "You are Nova" in prompt and "Mavis" not in prompt


async def test_system_prompt_without_name_asks_for_it(db) -> None:
    user, _ = await users.get_or_create_by_chat(2, None)
    assert "don't know their name yet" in system_prompt(user, NOW)
    assert "Ask once" in system_prompt(user, NOW)


async def test_name_ask_suppressed_when_known_or_asked_today(db) -> None:
    user, _ = await users.get_or_create_by_chat(5, None)
    assert "Never ask what to call them" in system_prompt(user, NOW, known_name="Jai")
    assert "Do not ask again" in system_prompt(user, NOW, ask_name=False)


def _msg(role, content, at):
    return SimpleNamespace(role=role, content=content, created_at=at)


def test_should_ask_name_logic() -> None:
    tz = "Asia/Kolkata"
    assert should_ask_name(None, [], NOW, tz)
    assert not should_ask_name("Jai", [], NOW, tz)
    asked = [_msg("assistant", "By the way, what should I call you?", NOW - timedelta(hours=1))]
    assert not should_ask_name(None, asked, NOW, tz)
    yesterday = [_msg("assistant", "what should I call you?", NOW - timedelta(days=1))]
    assert should_ask_name(None, yesterday, NOW, tz)
    user_said = [_msg("user", "call you what", NOW - timedelta(hours=1))]
    assert should_ask_name(None, user_said, NOW, tz)


async def test_no_reintroduction_when_mid_conversation(db) -> None:
    user, _ = await users.get_or_create_by_chat(6, "Jai")
    assert "brief hello is fine" in system_prompt(user, NOW, prior_turns=0)
    assert "hello" not in system_prompt(user, NOW).split("Right now")[1]  # None: no claim either way
    mid = system_prompt(user, NOW, prior_turns=4)
    assert "Do not introduce yourself" in mid and "brief hello is fine" not in mid


async def test_connection_state_is_injected(db) -> None:
    user, _ = await users.get_or_create_by_chat(7, "Jai")
    prompt = system_prompt(user, NOW, connections={"gmail": "connected", "googlecalendar": "not connected"})
    assert "Gmail: connected" in prompt
    assert "Google Calendar: not connected" in prompt and "/connect calendar" in prompt
    assert "Gmail: connected" in prompt and "/connect gmail" in prompt
    unknown = system_prompt(user, NOW, connections={})  # looked, could not tell
    assert "Gmail: unknown" in unknown and "Google Calendar: unknown" in unknown
    both = {"gmail": "pending", "googlecalendar": "needs reconnecting"}
    states = system_prompt(user, NOW, connections=both)
    assert "Gmail: pending" in states and "Google Calendar: needs reconnecting" in states
    # not injecting at all (proactive callers) leaves the block out entirely
    assert "Their links right now" not in system_prompt(user, NOW)


def test_split_bubbles() -> None:
    assert split_bubbles("Hey!\n---\nWhat's up?") == ["Hey!", "What's up?"]
    assert split_bubbles("Hey!\n\nWhat's up?") == ["Hey!\n\nWhat's up?"]  # paragraphs stay together
    assert split_bubbles("one\n---\n---\ntwo\n---\nthree\n---\nfour") == ["one", "two", "three\n\nfour"]
    assert split_bubbles("  single  ") == ["single"]
    assert split_bubbles("   ") == []


def test_recent_messages_and_name_ask_ignore_old_history() -> None:
    old = _msg("assistant", "what should I call you?", NOW - timedelta(hours=13))
    fresh = _msg("user", "hi", NOW - timedelta(hours=1))
    assert recent_messages([old, fresh], NOW) == [fresh]
    # asked 13h ago but still "today" locally: outside the 12h window, so asking is allowed again
    assert should_ask_name(None, [old], NOW, "Asia/Kolkata")


async def test_persona_carries_register_rules(db) -> None:
    user, _ = await users.get_or_create_by_chat(8, "Jai")
    prompt = system_prompt(user, NOW)
    low = prompt.lower()
    for rule in ("slur", "never insult", "sexual", "lecture", "canned refusal", "unbothered"):
        assert rule in low, rule
    assert "only when the register note" in low  # no note: no swearing
    assert "Their register right now" not in prompt


async def test_register_line_lands_in_right_now_section(db) -> None:
    user, _ = await users.get_or_create_by_chat(9, "Jai")
    line = "Their register right now: casual and sweary. You may swear casually back."
    prompt = system_prompt(user, NOW, register_line=line, context="## Known facts\n- x")
    assert line in prompt
    assert prompt.index("Right now") < prompt.index(line) < prompt.index("## Known facts")
