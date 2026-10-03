"""GOOGLE_WORKSPACE_ENABLED: off means the capability set, names and tool gating are exactly as before."""

from __future__ import annotations

from mavis.domain.errors import ConnectionRequired
from mavis.domain.policy import Capability, RiskClass
from mavis.tools.integrations.actions import (
    GOOGLE_CAPABILITIES,
    INTEGRATION_CAPABILITIES,
    WORKSPACE_CAPABILITIES,
    active_capabilities,
    display_name,
    is_google,
)
from mavis.tools.registry import MavisTool


def _tool(requires: Capability | None) -> MavisTool:
    async def fn(user_id, args):
        return "ok"

    from pydantic import BaseModel

    class A(BaseModel):
        pass

    return MavisTool(name="t", description="d", args_model=A, risk=RiskClass.READ, fn=fn,
                     agents=frozenset({"conversation"}), requires=requires)


def test_flag_defaults_off(settings):
    assert settings.google_workspace_enabled is False
    assert settings.workspace_poll_minutes == 30


def test_flag_off_keeps_the_old_four(settings):
    assert active_capabilities() == INTEGRATION_CAPABILITIES
    assert display_name(Capability.GMAIL) == "Gmail"
    assert display_name(Capability.CALENDAR) == "Google Calendar"
    assert not is_google(Capability.GMAIL)


def test_flag_on_adds_six_google_capabilities(workspace_on):
    assert active_capabilities() == INTEGRATION_CAPABILITIES + WORKSPACE_CAPABILITIES
    assert len(GOOGLE_CAPABILITIES) == 8
    assert {display_name(c) for c in GOOGLE_CAPABILITIES} == {"Google"}
    assert display_name(Capability.SLACK) == "Slack"
    assert Capability.GMAIL.value == "gmail" and Capability.CALENDAR.value == "googlecalendar"


def test_tool_available_hides_workspace_tools_when_off(settings):
    from mavis.tools.integrations.wiring import tool_available

    assert tool_available(_tool(Capability.DRIVE)) is False
    assert tool_available(_tool(Capability.WEB)) is True
    assert tool_available(_tool(None)) is True


def test_tool_available_offers_workspace_tools_when_on(workspace_on, monkeypatch):
    from mavis.config import get_settings
    from mavis.tools.integrations.wiring import tool_available

    monkeypatch.setenv("COMPOSIO_API_KEY", "ck_test_not_real")  # a configured provider; no call is made
    get_settings.cache_clear()
    assert tool_available(_tool(Capability.DRIVE)) is True


def test_connect_hint_names_google_when_on(workspace_on):
    from mavis.agents.conversation import _connect_hint

    text = _connect_hint(ConnectionRequired(Capability.DRIVE, "find and work with your Drive files"))
    assert text == "I need your Google linked for that. Send /connect google and I'll take it from there."


def test_connect_hint_unchanged_when_off(settings):
    from mavis.agents.conversation import _connect_hint

    text = _connect_hint(ConnectionRequired(Capability.CALENDAR, "work with your calendar"))
    assert text == ("I need your Google Calendar linked for that. "
                    "Send /connect calendar and I'll take it from there.")
