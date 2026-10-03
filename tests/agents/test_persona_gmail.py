from datetime import UTC, datetime

from mavis.agents.persona import system_prompt
from mavis.store.repo import users

NOW = datetime(2026, 10, 2, 4, 30, tzinfo=UTC)


async def test_prompt_says_gmail_reading_works(db):
    user, _ = await users.get_or_create_by_chat(5, "Jai")
    prompt = system_prompt(user, NOW)
    assert "what's new in their inbox" in prompt and '"was this you?"' in prompt
    assert prompt.index("what's new in their inbox") < prompt.index("On the way")
    assert "Coming next: sending email" not in prompt  # sending works now (Phase 4), with their OK
    assert "\u2014" not in prompt and "\u2013" not in prompt
