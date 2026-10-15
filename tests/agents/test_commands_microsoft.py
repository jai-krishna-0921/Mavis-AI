import pytest

from mavis.agents.commands import capabilities_named, capability_from_text, connect_word
from mavis.domain.policy import Capability as C


@pytest.fixture
def ms_on(ms_env):
    yield


@pytest.mark.parametrize("text", ["outlook", "connect my Outlook", "teams", "OneDrive", "microsoft 365",
                                  "office365", "m365", "outlook calendar", "outlook mail", "my sharepoint"])
def test_microsoft_words_open_the_microsoft_consent(ms_on, text):
    assert capability_from_text(text) is C.OUTLOOK


@pytest.mark.parametrize("text,expected", [
    ("outlook mail", {C.OUTLOOK}), ("outlook calendar and inbox", {C.OUTLOOK}),
    ("gmail and outlook", {C.GMAIL, C.OUTLOOK}), ("google calendar and teams", {C.CALENDAR, C.OUTLOOK}),
])
def test_named_sets(ms_on, text, expected):
    named = capabilities_named(text)
    assert expected <= named and (C.GMAIL in named) == (C.GMAIL in expected)


def test_google_words_are_unchanged(ms_on):
    assert capability_from_text("gmail") is C.GMAIL and capability_from_text("slack") is C.SLACK


def test_microsoft_words_are_ignored_when_it_is_not_enabled(settings):
    assert capability_from_text("teams") is None and capability_from_text("outlook") is None


def test_connect_word(ms_on):
    assert connect_word(C.TEAMS) == "microsoft" and connect_word(C.OUTLOOK_CALENDAR) == "microsoft"
