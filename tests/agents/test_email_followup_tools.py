"""Hotfix 3 RC6: after a brief mentioned an email, follow-ups must be able to look it up.

Prod transcript: Mavis briefed "Meetup TOS update"; the user asked "Whats that meetup updated TOS?" then
"Give me the key points?" and Mavis said it did not have the email text. Offline repro: for the first
message the 8-tool chat set offered mail_search but not mail_read (search returns short previews
only), and the prompt never said a briefed email is only a summary that must be looked up."""

from __future__ import annotations

import pytest

from mavis.agents import conversation

BRIEF = ("Morning! A quick look at your inbox: Meetup TOS update, your electricity bill is due Friday, "
         "and a Substack newsletter.")
ANSWERS = (
    "That's Meetup letting you know their Terms of Service are changing.",
    "Meetup sent a notice about updated terms. Want the key points?",
    "Not much detail in what I saw, just that Meetup's TOS is changing.",
)


@pytest.fixture
def real_catalog(settings, monkeypatch):
    from mavis.tools import registry

    monkeypatch.setattr(registry, "_REGISTRY", None)
    return registry.get_registry()


def _offered(query: str) -> set[str]:
    return {t.name for t in conversation.chat_tools(1, query)}


def test_question_about_a_briefed_email_offers_search_and_read(real_catalog) -> None:
    offered = _offered(f"Whats that meetup updated TOS?\n{BRIEF}")
    assert {"mail_search", "mail_read"} <= offered


@pytest.mark.parametrize("previous", ANSWERS)
def test_key_points_follow_up_offers_search_and_read(real_catalog, previous) -> None:
    offered = _offered(f"Give me the key points?\n{previous}")
    assert {"mail_search", "mail_read"} <= offered


def test_mail_read_always_comes_with_mail_search(real_catalog) -> None:
    for query in ("remind me to call mom", "search the web for flights", "what's on my calendar",
                  f"Whats that meetup updated TOS?\n{BRIEF}"):
        offered = _offered(query)
        assert ("mail_search" in offered) == ("mail_read" in offered), query
        assert len(offered) <= conversation.CHAT_TOOL_LIMIT


def test_prompt_says_a_briefed_email_must_be_looked_up() -> None:
    rules = conversation.TOOL_RULES.lower()
    assert "mentioned earlier" in rules and "mail_read" in rules
    assert "—" not in conversation.TOOL_RULES and "–" not in conversation.TOOL_RULES
