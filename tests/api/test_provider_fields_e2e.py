# tests/api/test_provider_fields_e2e.py
from __future__ import annotations

import ast
import dataclasses
import pathlib

import httpx
import pytest

from mavis.access import deletion
from mavis.domain.integrations import ConnectionState, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.connect_flow import ConnectFlow
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.providers import ProviderSpec
from mavis.tools.integrations.poller import Poller
from tests.api.dash_helpers import *  # noqa: F403 - fixtures and helpers
from tests.api.dash_helpers import query, signed_in
from tests.api.test_oauth_routes import outbox
from tests.tools.integrations.fakes import NOW, FakeBus, FakeState, Recorder
from tests.tools.integrations.native import fake_provider as fp
from tests.tools.integrations.native.conftest import form

API = "/api/v1"
T = NativeProvider.TESTING
N, SB = Capability.NOTION, Capability.SANDBOX
U = UserRef(user_id=1)


ROOT = pathlib.Path(__file__).resolve().parents[2]
ZZ = (frozenset({"zzz"}),)  # a scope the fake's grant never holds


def _defined(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def test_every_spec_field_has_a_probe():
    fields = {f.name for f in dataclasses.fields(ProviderSpec)}
    assert fields == set(fp.COVERAGE), (
        f"add a probe to COVERAGE for {sorted(fields - set(fp.COVERAGE))}; "
        f"remove stale {sorted(set(fp.COVERAGE) - fields)}")


def test_every_probe_names_a_test_that_exists():
    broken = []
    for field, probe in fp.COVERAGE.items():
        path, _, name = probe.partition("::")
        file = ROOT / path
        if not file.is_file() or name not in _defined(file):
            broken.append(f"{field} -> {probe}")
    assert broken == []


def test_the_fake_sets_every_field_to_a_non_default_value():
    spec = fp.make_spec()
    # probed by an override in their own test
    skipped = {"internal", "soft_errors", "legacy_capabilities", "companions", "executor"}
    same = []
    for f in dataclasses.fields(ProviderSpec):
        if f.name in skipped:
            continue
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            continue  # required field: always set
        default = f.default if f.default is not dataclasses.MISSING else f.default_factory()
        if getattr(spec, f.name) == default:
            same.append(f.name)
    assert same == []
    assert spec.family_enabled.__name__ != "_always"


@pytest.fixture
def fake_env(router, vendor, isolated_registry, monkeypatch):
    fp.add_actions(monkeypatch)
    fp.use_settings(monkeypatch)
    fp.CALLS.clear()
    isolated_registry.register(fp.make_spec())
    fp.install(vendor)
    router.executors[T] = fp.FakeExec()
    return router


def swap(**over):
    """Replace the registered fake with a variant (the registry is restored by isolated_registry)."""
    providers.unregister("testing")
    return providers.register(fp.make_spec(**over))


def flow_for(env, **kw) -> tuple[ConnectFlow, Recorder]:
    rec = Recorder()
    flow = ConnectFlow(provider=env, cache=ConnectionCache(env, ttl_s=0), bus=FakeBus(), notify=rec.notify,
                       schedule=rec.schedule, state=FakeState(), base_url="https://mavis.test", **kw)
    return flow, rec


async def callback(app, state: str, **params) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mavis.test") as c:
        return await c.get("/oauth/testing/callback", params={"state": state, **params})


async def test_connect_url_and_callback_page(app, fake_env, vendor, rec_bus):
    url = await fake_env.connect_link(U, "testco", "https://mavis.test/connect/callback?p=9")
    q = query(url)
    assert url.startswith("https://idp.test/authorize?")
    assert q["scope"] == "openid mail.read" and q["prompt"] == "select_account"
    assert q["code_challenge_method"] == "S256" and q["client_id"] == "tid"
    r = await callback(app, q["state"], code="c")
    assert r.status_code == 200 and "me@corp.test is now linked" in r.text and "Testco" in r.text
    assert form(vendor.to("https://idp.test/token")[0])["code_verifier"]
    assert ("after_save", 1) in fp.CALLS and ("remember", 1, "me@corp.test") in fp.CALLS
    assert await fake_env.tokens.owner_of_key(T, "t1/o1") == 1
    assert rec_bus.jobs[-1].payload == {"pending_id": 9}
    assert await fake_env.tokens.grant(1, NativeProvider.SLACK_BOT) is None  # no companions by default


async def test_status_and_execute_through_the_router(fake_env):
    await fp.grant(fake_env.tokens)
    states = await fake_env.status(U)
    assert states["notion"] is ConnectionState.ACTIVE and states["sandbox"] is ConnectionState.ACTIVE
    assert (await fake_env.execute(U, "notion.search", {})).data == {"native": "notion.search"}
    assert providers.family_of(N).id == "testing"
    await fake_env.tokens.save(1, T, account={"tid": "t1", "oid": "o1", "scopes": ["other"]},
                               access_token="a", refresh_token="r", expires_at=None)
    denied = await fake_env.execute(U, "notion.search", {})
    assert denied.ok is False and "reconnect" in denied.error


async def test_capabilities_for_trims_a_personal_grant(fake_env):
    await fp.grant(fake_env.tokens, plan="personal")
    states = await fake_env.status(U)
    assert states["notion"] is ConnectionState.ACTIVE and "sandbox" not in states


async def test_poll_hook_runs(fake_env):
    await fp.grant(fake_env.tokens)
    state, rec = FakeState(), Recorder()
    await state.update(1, {"polling": {"sandbox": True}})
    poller = Poller(provider=fake_env, cache=ConnectionCache(fake_env, ttl_s=0), bus=FakeBus(), state=state,
                    schedule=rec.schedule, clock=lambda: NOW)
    assert await poller.poll(1, SB) == 1
    assert (await state.get(1))["cursors"] == {"fake": "1"} and rec.scheduled


async def test_disconnect_forgets_the_prefixes_and_tells_the_vendor(fake_env):
    await fp.grant(fake_env.tokens)
    forgotten = []

    async def forget(user_id, prefix):
        forgotten.append(prefix)
        return 1

    flow, rec = flow_for(fake_env, forget_source=forget)
    assert await flow.disconnect(1, N) is True
    assert forgotten == ["tco:"] and await fake_env.tokens.grant(1, T) is None
    assert ("revoke", "a") in fp.CALLS and "I also removed what I had learned" in rec.sent[-1].text


async def test_account_deletion_revokes_the_fake_grant(fake_env, vendor):
    await fp.grant(fake_env.tokens, user=2)
    assert (await deletion._native(2))["revoked"] == 1
    assert await fake_env.tokens.grant(2, T) is None and len(vendor.to("https://idp.test/revoke")) == 1


async def test_revoke_goes_to_the_specs_revoke_url(fake_env, vendor):
    swap(revoke_url="https://idp.test/other-revoke")
    vendor.routes["https://idp.test/other-revoke"] = lambda r: httpx.Response(200, json={})
    await fp.grant(fake_env.tokens)
    await fake_env.oauth.revoke(1, T)
    assert len(vendor.to("https://idp.test/other-revoke")) == 1 and vendor.to("https://idp.test/revoke") == []


async def test_companion_grant_is_saved_on_connect_and_deleted_on_revoke(app, fake_env, vendor):
    swap(companions=("slack_bot",))
    SBOT = NativeProvider.SLACK_BOT
    q = query(await fake_env.connect_link(U, "testco", "https://mavis.test/connect/callback"))
    assert (await callback(app, q["state"], code="c")).status_code == 200
    assert await fake_env.tokens.grant(1, SBOT) is not None
    await fake_env.oauth.revoke(1, T)
    assert await fake_env.tokens.grant(1, SBOT) is None and await fake_env.tokens.grant(1, T) is None
    # and an account deletion sweeps both
    await fp.grant(fake_env.tokens, user=2)
    await fake_env.tokens.save(2, SBOT, account={"team_id": "T1", "scopes": []}, access_token="b",
                               refresh_token=None, expires_at=None)
    await deletion._native(2)
    assert await fake_env.tokens.grant(2, SBOT) is None


async def test_dashboard_card_and_connect(app, fake_env):
    c, csrf = await signed_in(app, 1)
    async with c:
        card = next(r for r in (await c.get(f"{API}/connectors")).json() if r["id"] == "testing")
        assert (card["name"], card["icon"], card["services"]) == ("Testco Suite", "testco", ["notes"])
        await fp.grant(fake_env.tokens)
        card = next(r for r in (await c.get(f"{API}/connectors")).json() if r["id"] == "testing")
        assert card["account"] == "me@corp.test" and card["status"] == "active"
        url = (await c.post(f"{API}/connectors/testing/connect", headers=csrf, json={})).json()["url"]
        assert url.startswith("https://idp.test/authorize?")


async def test_dashboard_disconnect_forgets_exactly_the_memory_sources(app, fake_env, monkeypatch):
    forgotten, prefixes = [], []

    class Vault:
        async def forget_source(self, user_id, source):
            forgotten.append(source)

    async def forget(user_id, prefix):
        prefixes.append(prefix)
        return 1

    flow, _ = flow_for(fake_env, forget_source=forget)
    monkeypatch.setattr("mavis.api.dashboard.connectors.get_vault", lambda: Vault())
    monkeypatch.setattr("mavis.tools.integrations.wiring.get_connect_flow", lambda: flow)
    swap(memory_sources={"tco": ("tco:",), "tco2": ("tco2:",)})
    await fp.grant(fake_env.tokens)
    c, csrf = await signed_in(app, 1)
    async with c:
        res = await c.post(f"{API}/connectors/testing/disconnect", headers=csrf, json={})
        assert res.status_code == 204
    assert sorted(forgotten) == ["tco", "tco2"] and prefixes == ["tco:"]


def test_memory_sources_feed_the_vault_and_the_controls(isolated_registry):
    from mavis.memory import controls, vault

    isolated_registry.register(fp.make_spec())
    assert vault.connector_prefixes()["tco"] == ("tco:",) and "tco" in controls.connectors()


async def test_refresh_sends_the_specs_params_and_credentials(fake_env, vendor):
    await fp.grant(fake_env.tokens, expired=True)
    vendor.fail = {"status": 400, "body": {"error": "other"}}
    with pytest.raises(Exception):  # noqa: B017, PT011 - any refresh failure; the request is what we read
        await fake_env.tokens.access_token(1, T)
    sent = form(vendor.to("https://idp.test/token")[-1])
    assert sent["scope"] == "mail.read offline_access"
    assert (sent["client_id"], sent["client_secret"]) == ("tid", "tsecret")


async def test_gone_codes_end_the_grant(fake_env, vendor):
    await fp.grant(fake_env.tokens, expired=True)
    vendor.fail = {"status": 400, "body": {"error": "interaction_required"}}
    with pytest.raises(ReauthRequired):
        await fake_env.tokens.access_token(1, T)
    swap(gone_codes=frozenset({"never"}))
    await fp.grant(fake_env.tokens, expired=True)
    with pytest.raises(Exception) as other:  # noqa: PT011
        await fake_env.tokens.access_token(1, T)
    assert not isinstance(other.value, ReauthRequired)


async def test_soft_errors_override_reads_a_200_error_body(fake_env, vendor):
    swap(soft_errors=True, gone_codes=frozenset({"token_revoked"}))
    await fp.grant(fake_env.tokens, expired=True)
    vendor.fail = {"status": 200, "body": {"ok": False, "error": "token_revoked"}}
    with pytest.raises(ReauthRequired):
        await fake_env.tokens.access_token(1, T)


async def test_admin_consent_denial_copy(app, fake_env):
    state = query(await fake_env.oauth.authorize_url(1, T))["state"]
    r = await callback(app, state, error="consent_required")
    assert r.status_code == 400 and "Testco needs an admin: https://idp.test/adminconsent" in r.text
    state = query(await fake_env.oauth.authorize_url(1, T))["state"]
    assert "needs an admin" not in (await callback(app, state, error="access_denied")).text


async def test_internal_spec_has_no_routes_card_or_listing(app, fake_env):
    c, _ = await signed_in(app, 1)
    async with c:
        assert (await c.get("/oauth/testing/go", params={"t": "x"})).status_code == 410  # a normal spec
        swap(internal=True, capabilities=(), anchor=None, pollers={}, toolkits=frozenset(), catalog=None,
             action_scopes={}, capabilities_for=None)
        assert (await c.get("/oauth/testing/go", params={"t": "x"})).status_code == 404
        assert (await c.get("/oauth/testing/callback", params={"code": "c", "state": "s"})).status_code == 404
        assert "testing" not in [r["id"] for r in (await c.get(f"{API}/connectors")).json()]
    assert "testing" not in [s.id for s in providers.user_providers()]
    assert providers.spec_of("testing") is not None


async def test_features_split_into_allowed_and_not(fake_env):
    await fp.grant(fake_env.tokens)
    assert await fake_env.provider_access(1, T) == (["search notes", "run boxes"], [])
    swap(action_scopes={"notion.search": (fp.NEED,), "sandbox.run": ZZ})
    assert await fake_env.provider_access(1, T) == (["search notes"], ["run boxes"])
    swap(features=())
    assert await fake_env.provider_access(1, T) is None


async def test_nothing_allowed_copy_and_revoke(app, fake_env):
    swap(action_scopes={"notion.search": ZZ, "sandbox.run": ZZ})
    state = query(await fake_env.oauth.authorize_url(1, T))["state"]
    r = await callback(app, state, code="c")
    assert r.status_code == 400 and "did not allow any Testco service" in r.text
    assert "You signed in with me@corp.test but allowed nothing." in await outbox()
    assert await fake_env.tokens.grant(1, T) is None


async def test_announcement_uses_abilities_and_the_reconnect_hint(fake_env):
    await fp.grant(fake_env.tokens)
    flow, _ = flow_for(fake_env)
    spec = providers.spec_of("testing")
    assert await flow._family_announcement(1, spec) == "Connected ✓ I can now work with your notes and boxes."
    swap(action_scopes={"notion.search": (fp.NEED,), "sandbox.run": ZZ})
    assert await flow._family_announcement(1, providers.spec_of("testing")) == (
        "Connected ✓ I can search notes. Not allowed on Testco's screen: run boxes. "
        "To add them, send /connect testing.")


async def test_menu_and_status_use_the_family_label(fake_env):
    await fp.grant(fake_env.tokens)
    flow, _ = flow_for(fake_env)
    assert "Testco Suite: connected" in await flow.status_text(1)
    swap(family_label="")
    assert "Testco: connected" in await flow.status_text(1)


async def test_family_gate_off_keeps_capabilities_apart(fake_env):
    await fp.grant(fake_env.tokens)
    flow, _ = flow_for(fake_env)
    assert await flow._fan_out(1, N) == [N, SB]
    swap(family_enabled=lambda: False)
    assert providers.family_of(N) is None and await flow._fan_out(1, N) == [N]
    assert "Testco Suite" not in await flow.status_text(1)


async def test_google_only_hooks_follow_legacy_capabilities(fake_env):
    await fp.grant(fake_env.tokens)
    calls = []

    async def active(user_id):
        calls.append("active")

    flow, _ = flow_for(fake_env, on_google_begin=lambda u: calls.append("begin"), on_google_active=active)
    await flow._activate(1, N)
    assert calls == []  # the fake has no legacy capabilities: Google's hooks stay silent
    swap(legacy_capabilities=(SB,))
    flow, _ = flow_for(fake_env, on_google_begin=lambda u: calls.append("begin"), on_google_active=active)
    await flow._activate(1, SB)
    assert "begin" in calls


