import pytest

from mavis.agents.commands import capabilities_named, capability_from_text, connect_word
from mavis.domain.policy import Capability as C
from mavis.tools.integrations.actions import active_capabilities, fathom_enabled


def test_fathom_keyword_maps_to_capability(fathom_env):
    assert capability_from_text("connect fathom") is C.FATHOM and capability_from_text("Fathom") is C.FATHOM
    assert C.FATHOM in capabilities_named("my fathom notes") and connect_word(C.FATHOM) == "fathom"
    assert C.FATHOM in active_capabilities()


def test_fathom_word_ignored_when_not_configured(settings):
    assert not fathom_enabled()
    assert capability_from_text("fathom") is None and C.FATHOM not in capabilities_named("fathom")
    assert C.FATHOM not in active_capabilities()


def test_fathom_tools_hidden_when_not_configured(settings):
    from types import SimpleNamespace

    from mavis.tools.integrations.wiring import tool_available

    assert tool_available(SimpleNamespace(requires=C.FATHOM)) is False


def test_fathom_tools_offered_when_configured(fathom_env):
    from types import SimpleNamespace

    from mavis.tools.integrations.wiring import tool_available

    assert tool_available(SimpleNamespace(requires=C.FATHOM)) is True


@pytest.mark.parametrize("text", ["notion", "slack"])
def test_other_words_unchanged(fathom_env, text):
    assert capability_from_text(text) is not C.FATHOM


def test_t3_connect_fathom_meeting_notes_is_fathom_not_the_calendar(fathom_env):
    assert capability_from_text("connect fathom meeting notes") is C.FATHOM
    assert capability_from_text("my meeting schedule") is C.CALENDAR
