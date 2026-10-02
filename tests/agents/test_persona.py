from datetime import UTC, datetime

from mavis.agents.persona import split_bubbles, system_prompt
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
    assert "Never use em dashes or en dashes" in prompt
    assert "No headings, no tables" in prompt
    assert "Available now:" in prompt and "Coming soon" in prompt
    for item in ("remember", "Gmail", "Morning check-ins", "decks"):
        assert item in prompt
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


def test_split_bubbles() -> None:
    assert split_bubbles("Hey!\n\nWhat's up?") == ["Hey!", "What's up?"]
    assert split_bubbles("one\n\n\n\ntwo\n\nthree\n\nfour") == ["one", "two", "three\n\nfour"]
    assert split_bubbles("  single  ") == ["single"]
    assert split_bubbles("   ") == []
