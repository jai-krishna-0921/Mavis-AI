import json

import httpx
import pytest
import respx

from mavis.domain.errors import IntegrationError
from mavis.domain.integrations import ConnectionState, UserRef
from mavis.tools.integrations.composio import ComposioProvider

BASE = "https://backend.composio.dev/api/v3"
KEY = "ck_live_SUPERSECRET"
USER = UserRef(user_id=7)


@pytest.fixture
def provider():
    return ComposioProvider(api_key=KEY, base_url=BASE)


def _acct(slug, status, created, acct_id, user="mavis-7"):
    return {
        "id": acct_id, "status": status, "created_at": created, "user_id": user,
        "toolkit": {"slug": slug},
    }


@respx.mock
async def test_status_newest_active_wins(provider):
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        _acct("gmail", "ACTIVE", "2026-10-01T00:00:00Z", "ca_old"),
        _acct("gmail", "FAILED", "2026-10-02T00:00:00Z", "ca_new"),
        _acct("slack", "FAILED", "2026-10-01T00:00:00Z", "ca_s1"),
        _acct("slack", "ACTIVE", "2026-10-02T00:00:00Z", "ca_s2"),
        _acct("notion", "ACTIVE", "2026-10-02T00:00:00Z", "ca_other", user="mavis-99"),
    ]}))
    states = await provider.status(USER)
    assert states["gmail"] is ConnectionState.ACTIVE
    assert states["slack"] is ConnectionState.ACTIVE
    assert states["notion"] is ConnectionState.NONE      # another identity's account is never ours
    assert states["googlecalendar"] is ConnectionState.NONE
    req = respx.calls.last.request
    assert req.url.params["user_ids"] == "mavis-7"
    assert req.headers["x-api-key"] == KEY


async def test_status_unconfigured_is_all_none():
    p = ComposioProvider(api_key="", base_url=BASE)
    states = await p.status(USER)
    assert set(states.values()) == {ConnectionState.NONE}
    assert p.configured is False


@respx.mock
async def test_connect_link_creates_managed_auth_config(provider):
    respx.get(f"{BASE}/auth_configs").mock(return_value=httpx.Response(200, json={"items": []}))
    create = respx.post(f"{BASE}/auth_configs").mock(
        return_value=httpx.Response(200, json={"auth_config": {"id": "ac_1"}})
    )
    link = respx.post(f"{BASE}/connected_accounts/link").mock(
        return_value=httpx.Response(200, json={"redirect_url": "https://accounts.google.com/consent"})
    )
    url = await provider.connect_link(USER, "gmail", "https://mavis.test/connect/callback?p=3")
    assert url == "https://accounts.google.com/consent"
    assert json.loads(create.calls.last.request.content) == {
        "toolkit": {"slug": "gmail"}, "auth_config": {"type": "use_composio_managed_auth"},
    }
    assert json.loads(link.calls.last.request.content) == {
        "auth_config_id": "ac_1", "user_id": "mavis-7",
        "callback_url": "https://mavis.test/connect/callback?p=3",
    }


@respx.mock
async def test_connect_link_reuses_enabled_config(provider):
    respx.get(f"{BASE}/auth_configs").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "ac_disabled", "status": "DISABLED"}, {"id": "ac_ok", "status": "ENABLED"},
    ]}))
    link = respx.post(f"{BASE}/connected_accounts/link").mock(
        return_value=httpx.Response(200, json={"redirect_url": "https://x"})
    )
    await provider.connect_link(USER, "slack", "https://cb")
    assert json.loads(link.calls.last.request.content)["auth_config_id"] == "ac_ok"


async def test_connect_link_rejects_unknown_toolkit(provider):
    with pytest.raises(IntegrationError, match="not one of"):
        await provider.connect_link(USER, "../../admin", "https://cb")


@respx.mock
async def test_execute_translates_and_returns_data(provider):
    route = respx.post(f"{BASE}/tools/execute/GMAIL_FETCH_EMAILS").mock(
        return_value=httpx.Response(200, json={"successful": True, "data": {"messages": [{"subject": "Hi"}]}})
    )
    res = await provider.execute(USER, "mail.search", {"query": "is:unread", "max_results": 3})
    assert res.ok and res.data == {"messages": [{"subject": "Hi"}]}
    assert json.loads(route.calls.last.request.content) == {
        "user_id": "mavis-7", "arguments": {"query": "is:unread", "max_results": 3},
    }


@respx.mock
async def test_execute_unsuccessful(provider):
    respx.post(f"{BASE}/tools/execute/GMAIL_FETCH_EMAILS").mock(
        return_value=httpx.Response(200, json={"successful": False, "error": "quota exceeded"})
    )
    res = await provider.execute(USER, "mail.search", {})
    assert not res.ok and res.error == "quota exceeded"


async def test_execute_unknown_action(provider):
    res = await provider.execute(USER, "mail.delete_everything", {})
    assert not res.ok and "unknown action" in res.error


async def test_execute_invalid_arguments(provider):
    res = await provider.execute(USER, "mail.read", {})
    assert not res.ok and "invalid arguments" in res.error


@respx.mock
async def test_http_error_never_leaks_api_key(provider):
    respx.post(f"{BASE}/tools/execute/GMAIL_FETCH_EMAILS").mock(
        return_value=httpx.Response(400, json={"error": f"bad request with key {KEY}"})
    )
    res = await provider.execute(USER, "mail.search", {})
    assert not res.ok
    assert KEY not in (res.error or "")
    assert "400" in res.error
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(500, text=KEY))
    with pytest.raises(IntegrationError) as exc:
        await provider.status(USER)
    assert KEY not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@respx.mock
async def test_network_error_is_integration_error(provider):
    respx.get(f"{BASE}/connected_accounts").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(IntegrationError, match="could not reach Composio: ConnectError"):
        await provider.status(USER)


@respx.mock
async def test_disconnect_deletes_own_account(provider):
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        _acct("gmail", "ACTIVE", "2026-10-01T00:00:00Z", "ca_1"),
    ]}))
    delete = respx.delete(f"{BASE}/connected_accounts/ca_1").mock(return_value=httpx.Response(200, json={}))
    await provider.disconnect(USER, "gmail")
    assert delete.called


@respx.mock
async def test_subscribe_uses_active_account(provider):
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        _acct("gmail", "ACTIVE", "2026-10-01T00:00:00Z", "ca_1"),
    ]}))
    up = respx.post(f"{BASE}/trigger_instances/GMAIL_NEW_GMAIL_MESSAGE/upsert").mock(
        return_value=httpx.Response(200, json={"trigger_id": "ti_9"})
    )
    assert await provider.subscribe(USER, "mail.new_message", {}) == "ti_9"
    assert json.loads(up.calls.last.request.content) == {"connected_account_id": "ca_1", "trigger_config": {}}


@respx.mock
async def test_subscribe_without_active_account_fails(provider):
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(IntegrationError, match="no ACTIVE"):
        await provider.subscribe(USER, "mail.new_message", {})


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>gateway</html>"),
        httpx.Response(200, json=["not", "a", "dict"]),
        httpx.Response(200, json="text"),
    ],
)
async def test_malformed_success_body_is_integration_error(provider, response):
    respx.post(url__startswith=f"{BASE}/tools/execute/").mock(return_value=response)
    result = await provider.execute(USER, "mail.search", {"query": "x"})
    assert result.ok is False
    assert KEY not in (result.error or "")
    respx.get(f"{BASE}/connected_accounts").mock(return_value=response)
    with pytest.raises(IntegrationError):
        await provider.status(USER)


# --- hotfix3 RC4: an abandoned connect link is "not connected", never "expired" -------------------
NEVER_STARTED = "Connection expired before authorization was started"
NOT_FINISHED = "Authorization was started but not completed within 10 minutes"


def _attempt(slug, created, acct_id, reason):
    return {**_acct(slug, "EXPIRED", created, acct_id), "status_reason": reason}


@respx.mock
@pytest.mark.parametrize("reason", [NEVER_STARTED, NOT_FINISHED])
async def test_abandoned_connect_attempt_is_not_connected(provider, reason):
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        _attempt("googlecalendar", "2026-10-03T08:05:35Z", "ca_1", reason),
        _attempt("googlecalendar", "2026-10-03T08:54:31Z", "ca_2", reason),
    ]}))
    assert (await provider.status(USER))["googlecalendar"] is ConnectionState.NONE


@respx.mock
async def test_abandoned_attempt_does_not_hide_a_really_expired_account(provider):
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        {**_acct("gmail", "EXPIRED", "2026-09-01T00:00:00Z", "ca_real"), "status_reason": "Token revoked"},
        _attempt("gmail", "2026-10-03T08:05:35Z", "ca_try", NEVER_STARTED),
    ]}))
    assert (await provider.status(USER))["gmail"] is ConnectionState.FAILED


@respx.mock
async def test_initiated_attempt_is_not_reported_as_failed(provider):
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        _acct("googlecalendar", "INITIATED", "2026-10-03T08:05:35Z", "ca_1"),
    ]}))
    assert (await provider.status(USER))["googlecalendar"] is not ConnectionState.FAILED
