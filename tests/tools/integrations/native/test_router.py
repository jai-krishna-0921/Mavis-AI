from datetime import timedelta

import httpx
import pytest

from mavis.domain import timeutil
from mavis.domain.errors import IntegrationError, NoSuchConnection
from mavis.domain.integrations import ConnectionState, ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import GOOGLE_REVOKE_URL, SLACK_REVOKE_URL
from mavis.tools.integrations.native.router import NativeRouter, _load_executors, covers
from tests.tools.integrations.fakes import FakeProvider

G, S = NativeProvider.GOOGLE, NativeProvider.SLACK
U = UserRef(user_id=1)
ALL_GOOGLE = ["openid", "https://www.googleapis.com/auth/gmail.readonly",
              "https://www.googleapis.com/auth/calendar.events",
              "https://www.googleapis.com/auth/drive.readonly",
              "https://www.googleapis.com/auth/contacts.readonly"]


class FakeExecutor:
    def __init__(self, provider, actions):
        self.provider, self._actions, self.calls = provider, set(actions), []

    def handles(self, action):
        return action in self._actions

    async def execute(self, user, action, args):
        self.calls.append((user.user_id, action))
        return ToolResult(ok=True, data={"native": action})


async def grant(tokens, provider, scopes, user=1, status="ACTIVE"):
    account = {"scopes": scopes, "email": f"u{user}@x.com", "user_id": f"U{user}"}
    await tokens.save(user, provider, account=account,
                      access_token="a", refresh_token="r", expires_at=timeutil.now() + timedelta(hours=1))
    if status != "ACTIVE":
        await tokens.mark(user, provider, status)


@pytest.fixture
def parts(tokens, oauth, client):
    fallback = FakeProvider()
    g = FakeExecutor(G, {"mail.search", "mail.send", "calendar.list", "drive.search", "docs.read"})
    s = FakeExecutor(S, {"slack.history", "slack.send"})
    return fallback, g, s, NativeRouter(fallback, tokens, oauth, [g, s], client)


async def test_connected_user_runs_natively(parts, tokens):
    fallback, g, s, router = parts
    await grant(tokens, G, ALL_GOOGLE)
    await grant(tokens, S, [])
    assert (await router.execute(U, "mail.search", {})).data == {"native": "mail.search"}
    assert (await router.execute(U, "slack.send", {})).data == {"native": "slack.send"}
    assert (await router.execute(U, "docs.read", {})).data == {"native": "docs.read"}
    assert fallback.executed == []


@pytest.mark.parametrize("action", ["mail.draft", "tasks.list", "meet.create", "notion.search",
                                    "slack.channels", "no.such.action"])
async def test_actions_the_executor_does_not_handle_fall_back(parts, tokens, action):
    fallback, g, s, router = parts
    await grant(tokens, G, ALL_GOOGLE)
    await grant(tokens, S, [])
    await router.execute(U, action, {})
    assert [e[1] for e in fallback.executed] == [action] and not g.calls and not s.calls


async def test_user_without_a_grant_falls_back(parts):
    fallback, g, s, router = parts
    await router.execute(U, "mail.search", {})
    assert len(fallback.executed) == 1 and not g.calls


@pytest.mark.parametrize("status", ["REVOKED", "FAILED"])
async def test_inactive_grant_falls_back(parts, tokens, status):
    fallback, g, s, router = parts
    await grant(tokens, G, ALL_GOOGLE, status=status)
    await router.execute(U, "mail.search", {})
    assert len(fallback.executed) == 1 and not g.calls


async def test_grants_are_per_user(parts, tokens):
    fallback, g, s, router = parts
    await grant(tokens, G, ALL_GOOGLE, user=2)
    await router.execute(U, "mail.search", {})
    await router.execute(UserRef(user_id=2), "mail.search", {})
    assert len(fallback.executed) == 1 and g.calls == [(2, "mail.search")]


async def test_unticked_scopes_route_that_capability_to_composio(parts, tokens):
    fallback, g, s, router = parts
    await grant(tokens, G, ["openid", "https://www.googleapis.com/auth/gmail.readonly"])
    await router.execute(U, "mail.search", {})
    await router.execute(U, "calendar.list", {})
    await router.execute(U, "drive.search", {})
    assert [c[1] for c in g.calls] == ["mail.search"]
    assert [e[1] for e in fallback.executed] == ["calendar.list", "drive.search"]


async def test_missing_executor_routes_to_composio(tokens, oauth, client):
    fallback = FakeProvider()
    router = NativeRouter(fallback, tokens, oauth, [], client)
    await grant(tokens, G, ALL_GOOGLE)
    await router.execute(U, "mail.search", {})
    assert len(fallback.executed) == 1
    assert (await router.status(U))["gmail"] is not ConnectionState.ACTIVE


async def test_unconfigured_native_routes_to_composio(parts, tokens, monkeypatch):
    from mavis.config import get_settings
    fallback, g, s, router = parts
    await grant(tokens, G, ALL_GOOGLE)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    get_settings.cache_clear()
    await router.execute(U, "mail.search", {})
    assert len(fallback.executed) == 1 and not g.calls


async def test_status_merges_native_and_composio(parts, tokens):
    fallback, g, s, router = parts
    fallback.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)  # legacy Composio account
    fallback.set_state(1, Capability.NOTION, ConnectionState.ACTIVE)
    await grant(tokens, G, ["openid", "https://www.googleapis.com/auth/gmail.readonly"])
    await grant(tokens, S, [])
    st = await router.status(U)
    assert st["gmail"] is ConnectionState.ACTIVE and st["slack"] is ConnectionState.ACTIVE
    assert st["googlecalendar"] is ConnectionState.ACTIVE  # from Composio: not covered natively
    assert st["notion"] is ConnectionState.ACTIVE
    assert st.get("drive") is not ConnectionState.ACTIVE


async def test_status_full_google_grant_activates_all_native_capabilities(parts, tokens):
    _, _, _, router = parts
    await grant(tokens, G, ALL_GOOGLE)
    st = await router.status(U)
    for cap in ("gmail", "googlecalendar", "drive", "docs", "sheets", "contacts"):
        assert st[cap] is ConnectionState.ACTIVE
    assert st.get("tasks") is not ConnectionState.ACTIVE and st.get("meet") is not ConnectionState.ACTIVE


async def test_revoked_grant_shows_failed_unless_composio_is_active(parts, tokens):
    fallback, _, _, router = parts
    await grant(tokens, G, ALL_GOOGLE, status="REVOKED")
    fallback.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)
    st = await router.status(U)
    assert st["gmail"] is ConnectionState.FAILED
    assert st["googlecalendar"] is ConnectionState.ACTIVE


async def test_status_survives_composio_failure(tokens, oauth, client):
    class Down(FakeProvider):
        async def status(self, user):
            raise IntegrationError("down")
    router = NativeRouter(Down(), tokens, oauth, [FakeExecutor(G, {"mail.search"})], client)
    await grant(tokens, G, ALL_GOOGLE)
    assert (await router.status(U))["gmail"] is ConnectionState.ACTIVE


@pytest.mark.parametrize("toolkit,host", [("gmail", "accounts.google.com"),
                                          ("googlecalendar", "accounts.google.com"),
                                          ("drive", "accounts.google.com"), ("google", "accounts.google.com"),
                                          ("slack", "slack.com")])
async def test_connect_link_is_native_for_google_and_slack(parts, toolkit, host):
    fallback, *_, router = parts
    url = await router.connect_link(U, toolkit, "https://mavis.test/connect/callback?p=42")
    assert host in url and fallback.links == []


async def test_connect_link_carries_the_pending_id(parts, oauth, vendor):
    from .test_oauth import google_vendor, query
    *_, router = parts
    google_vendor(vendor)
    url = await router.connect_link(U, "gmail", "https://mavis.test/connect/callback?p=42")
    done = await oauth.complete(query(url)["state"], "c", G)
    assert done.pending_id == 42


@pytest.mark.parametrize("toolkit", ["notion", "tasks", "meet"])
async def test_connect_link_stays_on_composio_for_the_rest(parts, toolkit):
    fallback, *_, router = parts
    await router.connect_link(U, toolkit, "https://mavis.test/cb")
    assert fallback.links == [(1, toolkit, "https://mavis.test/cb")]


async def test_connect_link_without_native_config_uses_composio(parts, monkeypatch):
    from mavis.config import get_settings
    fallback, *_, router = parts
    monkeypatch.setenv("SLACK_CLIENT_ID", "")
    get_settings.cache_clear()
    await router.connect_link(U, "slack", "https://mavis.test/cb")
    assert len(fallback.links) == 1


async def test_disconnect_revokes_and_deletes_the_native_grant(parts, tokens, vendor):
    fallback, *_, router = parts
    vendor.routes[GOOGLE_REVOKE_URL] = lambda r: httpx.Response(200, json={})
    await grant(tokens, G, ALL_GOOGLE)
    await router.disconnect(U, "gmail")
    assert await tokens.grant(1, G) is None and len(vendor.to(GOOGLE_REVOKE_URL)) == 1
    assert fallback.disconnected == [(1, "gmail")]  # a legacy account for the service goes too


async def test_disconnect_tolerates_no_composio_account(tokens, oauth, client, vendor):
    class NoAccount(FakeProvider):
        async def disconnect(self, user, toolkit):
            raise NoSuchConnection("none")
    router = NativeRouter(NoAccount(), tokens, oauth, [FakeExecutor(S, set())], client)
    vendor.routes[SLACK_REVOKE_URL] = lambda r: httpx.Response(200, json={"ok": True})
    await grant(tokens, S, [])
    await router.disconnect(U, "slack")
    assert await tokens.grant(1, S) is None


async def test_disconnect_without_native_grant_goes_to_composio(parts):
    fallback, *_, router = parts
    await router.disconnect(U, "notion")
    await router.disconnect(U, "gmail")
    assert fallback.disconnected == [(1, "notion"), (1, "gmail")]


async def test_subscribe_parse_catalog_and_extras_delegate(parts):
    fallback, *_, router = parts
    assert await router.subscribe(U, "gmail.new", {}) == "ti_1"
    assert router.parse_webhook({}, b"") == []
    assert [t.slug for t in await router.catalog()]
    assert await router.retire_legacy_triggers(U) == 0  # Composio-only extra still reachable


def test_covers_rules():
    from mavis.tools.integrations.native.tokens import Grant
    g = Grant(1, G, "ACTIVE", {"scopes": ALL_GOOGLE})
    assert covers(g, Capability.DOCS) and not covers(g, Capability.TASKS) and not covers(g, Capability.SLACK)
    assert covers(Grant(1, S, "ACTIVE", {}), Capability.SLACK)
    assert not covers(Grant(1, S, "ACTIVE", {}), Capability.GMAIL)


def test_load_executors_skips_absent_modules_but_not_broken_ones(monkeypatch, client):
    import importlib

    seen = []

    def fake_import(name):
        seen.append(name)
        raise ModuleNotFoundError(name=name)
    monkeypatch.setattr(importlib, "import_module", fake_import)
    assert _load_executors(None, client) == [] and len(seen) == 2

    def broken(name):
        raise ModuleNotFoundError(name="some_missing_dependency")
    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        _load_executors(None, client)
