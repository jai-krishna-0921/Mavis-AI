from mavis.domain.policy import Capability as C
from mavis.tools.integrations.actions import active_capabilities, microsoft_enabled
from mavis.tools.integrations.wiring import tool_available
from mavis.tools.registry import get_registry


def tool(name):
    return get_registry().get(name)


def test_off_without_credentials(settings):
    assert not microsoft_enabled() and C.TEAMS not in active_capabilities()


def test_on_with_credentials(ms_env):
    assert microsoft_enabled()
    assert {C.OUTLOOK, C.TEAMS, C.ONEDRIVE, C.OUTLOOK_CALENDAR} <= set(active_capabilities())


def test_teams_and_onedrive_tools_follow_the_gate(ms_env, monkeypatch):
    assert tool_available(tool("teams_chats")) and tool_available(tool("onedrive_search"))
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "")
    from mavis.config import get_settings
    get_settings.cache_clear()
    assert not tool_available(tool("teams_chats")) and not tool_available(tool("onedrive_read"))


def test_mail_tools_follow_either_provider(ms_env):
    assert tool_available(tool("mail_search"))
