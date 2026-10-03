"""googlesuper routing in the Composio adapter (spec 2026-10-03 section 3.2). No network: respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mavis.domain.errors import IntegrationError
from mavis.domain.integrations import ConnectionState, UserRef
from mavis.tools.integrations.composio import ComposioProvider
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS, slug_for

BASE = "https://backend.composio.dev/api/v3"
USER = UserRef(user_id=7)
GOOGLE = ("gmail", "googlecalendar", "drive", "docs", "sheets", "tasks", "contacts", "meet")


def _acct(slug, status, acct_id, created="2026-10-01T00:00:00Z"):
    return {"id": acct_id, "status": status, "created_at": created, "user_id": "mavis-7",
            "toolkit": {"slug": slug}}


def _accounts(*items):
    return respx.get(f"{BASE}/connected_accounts").mock(
        return_value=httpx.Response(200, json={"items": list(items)})
    )


@pytest.fixture
def ws():
    return ComposioProvider(api_key="ck_test", base_url=BASE, workspace=True)


def test_slug_for_swaps_only_the_prefix():
    assert slug_for("mail.search", "googlesuper") == "GOOGLESUPER_FETCH_EMAILS"
    assert slug_for("mail.search", "gmail") == "GMAIL_FETCH_EMAILS"
    assert slug_for("calendar.create_event", "googlesuper") == "GOOGLESUPER_CREATE_EVENT"
    assert COMPOSIO_ACTIONS["calendar.list"].suffix == "EVENTS_LIST"


@respx.mock
async def test_status_all_google_capabilities_follow_googlesuper(ws):
    _accounts(_acct("googlesuper", "ACTIVE", "ca_g"), _acct("gmail", "ACTIVE", "ca_l"))
    states = await ws.status(USER)
    assert all(states[c] is ConnectionState.ACTIVE for c in GOOGLE)
    assert states["slack"] is ConnectionState.NONE
    assert "googlesuper" not in states


@respx.mock
async def test_status_legacy_only_keeps_gmail_and_leaves_drive_unconnected(ws):
    _accounts(_acct("gmail", "ACTIVE", "ca_l"))
    states = await ws.status(USER)
    assert states["gmail"] is ConnectionState.ACTIVE
    assert states["googlecalendar"] is ConnectionState.NONE
    assert states["drive"] is ConnectionState.NONE and states["tasks"] is ConnectionState.NONE


@respx.mock
async def test_googlesuper_auth_failure_marks_every_google_capability_failed(ws):
    _accounts(_acct("googlesuper", "EXPIRED", "ca_g"))
    states = await ws.status(USER)
    assert {states[c] for c in GOOGLE} == {ConnectionState.FAILED}


@respx.mock
async def test_execute_routes_gmail_to_googlesuper_when_both_active(ws):
    _accounts(_acct("googlesuper", "ACTIVE", "ca_g"), _acct("gmail", "ACTIVE", "ca_l"))
    route = respx.post(f"{BASE}/tools/execute/GOOGLESUPER_FETCH_EMAILS").mock(
        return_value=httpx.Response(200, json={"successful": True, "data": {"messages": []}})
    )
    res = await ws.execute(USER, "mail.search", {"query": "is:unread", "max_results": 3})
    assert res.ok and route.called
    sent = json.loads(route.calls.last.request.content)["arguments"]
    assert sent == {"query": "is:unread", "max_results": 3}


@respx.mock
async def test_execute_routes_gmail_to_legacy_when_only_legacy(ws):
    _accounts(_acct("gmail", "ACTIVE", "ca_l"))
    route = respx.post(f"{BASE}/tools/execute/GMAIL_FETCH_EMAILS").mock(
        return_value=httpx.Response(200, json={"successful": True, "data": {}})
    )
    assert (await ws.execute(USER, "mail.search", {})).ok and route.called


@respx.mock
async def test_execute_reuses_account_states_within_the_ttl(ws):
    accounts = _accounts(_acct("googlesuper", "ACTIVE", "ca_g"))
    respx.post(f"{BASE}/tools/execute/GOOGLESUPER_FETCH_EMAILS").mock(
        return_value=httpx.Response(200, json={"successful": True, "data": {}})
    )
    await ws.execute(USER, "mail.search", {})
    await ws.execute(USER, "mail.search", {})
    assert accounts.call_count == 1


@respx.mock
async def test_flag_off_never_routes_to_googlesuper():
    off = ComposioProvider(api_key="ck_test", base_url=BASE)
    accounts = _accounts(_acct("googlesuper", "ACTIVE", "ca_g"))
    route = respx.post(f"{BASE}/tools/execute/GMAIL_FETCH_EMAILS").mock(
        return_value=httpx.Response(200, json={"successful": True, "data": {}})
    )
    assert (await off.execute(USER, "mail.search", {})).ok and route.called
    assert not accounts.called  # flag off: no extra lookup, identical to before
    assert "drive" not in await off.status(USER)


@respx.mock
@pytest.mark.parametrize("name", ["google", "gmail", "drive", "tasks"])
async def test_connect_link_for_any_google_name_opens_googlesuper(ws, name):
    configs = respx.get(f"{BASE}/auth_configs").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "ac_g", "status": "ENABLED"}]})
    )
    link = respx.post(f"{BASE}/connected_accounts/link").mock(
        return_value=httpx.Response(200, json={"redirect_url": "https://accounts.google.com/x"})
    )
    assert await ws.connect_link(USER, name, "https://cb") == "https://accounts.google.com/x"
    assert configs.calls.last.request.url.params["toolkit_slug"] == "googlesuper"
    assert json.loads(link.calls.last.request.content)["auth_config_id"] == "ac_g"


@respx.mock
async def test_disconnect_google_removes_only_googlesuper(ws):
    _accounts(_acct("googlesuper", "ACTIVE", "ca_g"), _acct("gmail", "ACTIVE", "ca_l"))
    drop_g = respx.delete(f"{BASE}/connected_accounts/ca_g").mock(return_value=httpx.Response(200, json={}))
    drop_l = respx.delete(f"{BASE}/connected_accounts/ca_l").mock(return_value=httpx.Response(200, json={}))
    await ws.disconnect(USER, "google")
    assert drop_g.called and not drop_l.called


@respx.mock
async def test_disconnect_gmail_legacy_removes_the_old_account(ws):
    _accounts(_acct("googlesuper", "ACTIVE", "ca_g"), _acct("gmail", "ACTIVE", "ca_l"))
    drop_l = respx.delete(f"{BASE}/connected_accounts/ca_l").mock(return_value=httpx.Response(200, json={}))
    await ws.disconnect(USER, "gmail-legacy")
    assert drop_l.called


@respx.mock
async def test_subscribe_attaches_to_googlesuper_when_active(ws):
    _accounts(_acct("googlesuper", "ACTIVE", "ca_g"), _acct("gmail", "ACTIVE", "ca_l"))
    up = respx.post(f"{BASE}/trigger_instances/GOOGLESUPER_NEW_MESSAGE/upsert").mock(
        return_value=httpx.Response(200, json={"trigger_id": "ti_g"})
    )
    assert await ws.subscribe(USER, "mail.new_message", {}) == "ti_g"
    assert json.loads(up.calls.last.request.content)["connected_account_id"] == "ca_g"


@respx.mock
async def test_subscribe_falls_back_to_legacy_mail_trigger(ws):
    _accounts(_acct("gmail", "ACTIVE", "ca_l"))
    up = respx.post(f"{BASE}/trigger_instances/GMAIL_NEW_GMAIL_MESSAGE/upsert").mock(
        return_value=httpx.Response(200, json={"trigger_id": "ti_l"})
    )
    assert await ws.subscribe(USER, "mail.new_message", {}) == "ti_l" and up.called


@respx.mock
async def test_workspace_trigger_needs_googlesuper(ws):
    _accounts(_acct("gmail", "ACTIVE", "ca_l"))
    with pytest.raises(IntegrationError, match="no ACTIVE googlesuper"):
        await ws.subscribe(USER, "drive.file_shared", {})


@respx.mock
async def test_retire_legacy_triggers_deletes_only_gmail_and_calendar(ws):
    respx.get(f"{BASE}/trigger_instances/active").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "ti_1", "trigger_name": "GMAIL_NEW_GMAIL_MESSAGE", "user_id": "mavis-7"},
        {"id": "ti_2", "trigger_name": "GOOGLECALENDAR_GOOGLE_CALENDAR_EVENT_CHANGE_TRIGGER",
         "user_id": "mavis-7"},
        {"id": "ti_3", "trigger_name": "GOOGLESUPER_NEW_MESSAGE", "user_id": "mavis-7"},
        {"id": "ti_4", "trigger_name": "SLACK_RECEIVE_MESSAGE", "user_id": "mavis-7"},
        {"id": "ti_5", "trigger_name": "GMAIL_NEW_GMAIL_MESSAGE", "user_id": "mavis-99"},
    ]}))
    d1 = respx.delete(f"{BASE}/trigger_instances/manage/ti_1").mock(return_value=httpx.Response(200, json={}))
    d2 = respx.delete(f"{BASE}/trigger_instances/manage/ti_2").mock(return_value=httpx.Response(200, json={}))
    assert await ws.retire_legacy_triggers(USER) == 2
    assert d1.called and d2.called


@respx.mock
async def test_drive_with_legacy_only_raises_connection_required(ws):
    from mavis.domain.errors import ConnectionRequired
    from mavis.domain.policy import Capability
    from mavis.tools.integrations.connections import ConnectionCache

    _accounts(_acct("gmail", "ACTIVE", "ca_l"))
    cache = ConnectionCache(ws)
    await cache.ensure(7, Capability.GMAIL, "check and handle your email")  # legacy still works
    with pytest.raises(ConnectionRequired) as exc:
        await cache.ensure(7, Capability.DRIVE, "find and work with your Drive files")
    assert exc.value.capability is Capability.DRIVE and exc.value.revoked is False


@respx.mock
@pytest.mark.parametrize("name,acct", [("gmail", "ca_l"), ("googlecalendar", "ca_c")])
async def test_disconnect_legacy_only_user_removes_the_legacy_row(ws, name, acct):
    _accounts(_acct("gmail", "ACTIVE", "ca_l"), _acct("googlecalendar", "ACTIVE", "ca_c"))
    drop = respx.delete(f"{BASE}/connected_accounts/{acct}").mock(return_value=httpx.Response(200, json={}))
    await ws.disconnect(USER, name)
    assert drop.called


@respx.mock
async def test_disconnect_drops_route_cache_after_delete(ws):
    _accounts(_acct("googlesuper", "ACTIVE", "ca_g"))
    respx.delete(f"{BASE}/connected_accounts/ca_g").mock(return_value=httpx.Response(200, json={}))
    await ws.disconnect(USER, "google")
    assert USER.provider_id not in ws._routes


@respx.mock
async def test_legacy_active_but_googlesuper_expired_marks_every_google_capability_failed(ws):
    # legacy triggers were retired at the upgrade: reporting Gmail ACTIVE would hide that intake stopped
    _accounts(_acct("googlesuper", "EXPIRED", "ca_g"), _acct("gmail", "ACTIVE", "ca_l"),
              _acct("googlecalendar", "ACTIVE", "ca_c"))
    states = await ws.status(USER)
    assert {states[c] for c in GOOGLE} == {ConnectionState.FAILED}


@respx.mock
async def test_execute_does_not_fall_back_to_legacy_when_googlesuper_expired(ws):
    _accounts(_acct("googlesuper", "EXPIRED", "ca_g"), _acct("gmail", "ACTIVE", "ca_l"))
    legacy = respx.post(f"{BASE}/tools/execute/GMAIL_FETCH_EMAILS").mock(
        return_value=httpx.Response(200, json={"successful": True, "data": {}})
    )
    google = respx.post(f"{BASE}/tools/execute/GOOGLESUPER_FETCH_EMAILS").mock(
        return_value=httpx.Response(200, json={"successful": False, "error": "expired"})
    )
    res = await ws.execute(USER, "mail.search", {})
    assert not legacy.called and google.called and not res.ok
