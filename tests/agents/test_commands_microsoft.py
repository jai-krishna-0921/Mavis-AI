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
    ("gmail and outlook", {C.GMAIL, C.OUTLOOK}), ("google calendar and teams chats", {C.CALENDAR, C.OUTLOOK}),
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


@pytest.mark.parametrize("text,cap,expected", [
    ("check my Teams chats", C.TEAMS, True), ("check my Teams chats", C.ONEDRIVE, False),
    ("what's on my Outlook calendar", C.OUTLOOK_CALENDAR, True),
    ("what's on my Outlook calendar", C.TEAMS, False),
    ("find the deck in OneDrive", C.ONEDRIVE, True), ("find the deck in OneDrive", C.TEAMS, False),
    ("summarise my Microsoft 365", C.TEAMS, True), ("read my gmail", C.TEAMS, False),
])
def test_capability_requested_follows_the_microsoft_member(ms_on, text, cap, expected):
    from mavis.agents.commands import capability_requested
    assert capability_requested(cap, text) is expected


@pytest.mark.parametrize("text", ["our teams met yesterday", "football teams are fun"])
def test_ordinary_teams_is_not_a_connect_request(ms_on, text):
    assert capability_from_text(text) is None


@pytest.mark.parametrize("text", ["connect teams", "please link my teams", "teams", "Microsoft Teams"])
def test_teams_as_a_service_is_a_connect_request(ms_on, text):
    assert capability_from_text(text) is C.OUTLOOK


def test_other_google_words_keep_the_google_anchor(ms_on):
    assert {C.OUTLOOK, C.DRIVE} <= capabilities_named("outlook and sheets")
    assert {C.OUTLOOK, C.DRIVE} <= capabilities_named("teams chats and docs")


async def test_connect_my_email_offers_both_providers(ms_env, db):
    from mavis.agents.commands import handle_connect
    sent = []

    class Flow:
        provider = type("P", (), {"configured": True})()

        async def send(self, uid, text, buttons=None):
            sent.append((text, [b.label for row in buttons or [] for b in row]))

        async def offer_menu(self, uid):
            raise AssertionError("expected a two-button choice")

    await handle_connect(1, "connect my email", flow=Flow())
    assert sent == [("Which email should I hook up?", ["Connect Google", "Connect Microsoft 365"])]
