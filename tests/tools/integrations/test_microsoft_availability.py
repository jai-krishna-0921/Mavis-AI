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


async def _grant(uid, tid):
    from mavis.store import db as dbm
    from mavis.store.models import NativeGrant
    scopes = ["mail.readwrite", "calendars.readwrite", "chat.readwrite", "files.read.all"]
    async with dbm.Session() as s:
        s.add(NativeGrant(user_id=uid, provider="microsoft", account_key=f"{tid}/o", status="ACTIVE",
                          account={"tid": tid, "oid": "o", "scopes": scopes}, access_token="x"))
        await s.commit()


async def test_personal_account_loses_teams_tools_but_a_work_account_keeps_them(ms_env, user):
    from mavis.tools.integrations.native.microsoft_spec import PERSONAL_TENANT
    from mavis.tools.integrations.wiring import microsoft_unserved
    assert await microsoft_unserved(user.id) == frozenset()  # no account: connect prompt
    await _grant(user.id, PERSONAL_TENANT)
    assert await microsoft_unserved(user.id) == {C.TEAMS}
    names = [t.name for t in get_registry().for_agent("conversation", user.id,
                                                      exclude_capabilities=await microsoft_unserved(user.id))]
    assert "teams_chats" not in names and "onedrive_search" in names


async def test_work_account_is_offered_everything(ms_env, user):
    from mavis.tools.integrations.wiring import microsoft_unserved
    await _grant(user.id, "11111111-2222-3333-4444-555555555555")
    assert await microsoft_unserved(user.id) == frozenset()
