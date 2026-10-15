# tests/api/test_provider_fields_e2e.py
from __future__ import annotations

import dataclasses
import sys

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
from tests.tools.integrations.fakes import NOW, FakeBus, FakeState, Recorder
from tests.tools.integrations.native import fake_provider as fp
from tests.tools.integrations.native.conftest import form

API = "/api/v1"
T = NativeProvider.TESTING
N, SB = Capability.NOTION, Capability.SANDBOX
U = UserRef(user_id=1)


def test_every_spec_field_has_a_probe():
    fields = {f.name for f in dataclasses.fields(ProviderSpec)}
    assert fields == set(fp.COVERAGE), (
        f"add a probe to COVERAGE for {sorted(fields - set(fp.COVERAGE))}; "
        f"remove stale {sorted(set(fp.COVERAGE) - fields)}")


def test_every_probe_is_a_test_in_this_module():
    mod = sys.modules[__name__]
    missing = {f: p for f, p in fp.COVERAGE.items() if not callable(getattr(mod, p, None))}
    assert missing == {}


def test_the_fake_sets_every_field_but_the_documented_exceptions():
    spec, bare = fp.make_spec(), ProviderSpec(id="x", display_name="x", brand="x")
    skipped = {"internal", "soft_errors", "legacy_capabilities", "companions", "executor"}
    same = [f.name for f in dataclasses.fields(ProviderSpec)
            if f.name not in skipped and getattr(spec, f.name) == getattr(bare, f.name)]
    assert same == []


@pytest.fixture
def fake_env(router, vendor, isolated_registry, monkeypatch):
    fp.add_actions(monkeypatch)
    fp.CALLS.clear()
    isolated_registry.register(fp.make_spec())
    fp.install(vendor)
    router.executors[T] = fp.FakeExec()
    return router


async def test_connect_url_and_callback_page(app, fake_env, vendor, rec_bus):
    url = await fake_env.connect_link(U, "testco", "https://mavis.test/connect/callback?p=9")
    q = query(url)
    assert url.startswith("https://idp.test/authorize?")
    assert q["scope"] == "openid mail.read" and q["prompt"] == "select_account"
    assert q["code_challenge_method"] == "S256" and q["client_id"] == "sid"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mavis.test") as c:
        r = await c.get("/oauth/testing/callback", params={"code": "c", "state": q["state"]})
    assert r.status_code == 200 and "me@corp.test is now linked" in r.text and "Testco" in r.text
    assert form(vendor.to("https://idp.test/token")[0])["code_verifier"]
    assert ("after_save", 1) in fp.CALLS and ("remember", 1, "me@corp.test") in fp.CALLS
    assert await fake_env.tokens.owner_of_key(T, "t1/o1") == 1
    assert rec_bus.jobs[-1].payload == {"pending_id": 9}


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


async def test_disconnect_forgets_and_deletion_revokes(fake_env, vendor):
    await fp.grant(fake_env.tokens)
    forgotten, rec = [], Recorder()

    async def forget(user_id, prefix):
        forgotten.append(prefix)
        return 1

    flow = ConnectFlow(provider=fake_env, cache=ConnectionCache(fake_env, ttl_s=0), bus=FakeBus(),
                       notify=rec.notify, schedule=rec.schedule, state=FakeState(), base_url="https://mavis.test",
                       forget_source=forget)
    assert await flow.disconnect(1, N) is True
    assert forgotten == ["tco:"] and await fake_env.tokens.grant(1, T) is None
    assert ("revoke", "a") in fp.CALLS and "notes and boxes" not in rec.sent[-1].text
    await fp.grant(fake_env.tokens, user=2)
    assert (await deletion._native(2))["revoked"] == 1
    assert providers.spec_of("testing").family_label == "Testco Suite"
    assert providers.spec_of("testing").reconnect_hint == "send /connect testing"


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


async def test_refresh_gone_codes_and_params(fake_env, vendor):
    await fp.grant(fake_env.tokens, expired=True)
    vendor.fail = {"status": 400, "body": {"error": "interaction_required"}}
    with pytest.raises(ReauthRequired):
        await fake_env.tokens.access_token(1, T)
    sent = form(vendor.to("https://idp.test/token")[-1])
    assert sent["scope"] == "mail.read offline_access" and sent["client_secret"] == "ssecret"
    providers.unregister("testing")
    providers.register(fp.make_spec(soft_errors=True, gone_codes=frozenset({"token_revoked"})))
    await fp.grant(fake_env.tokens, expired=True)
    vendor.fail = {"status": 200, "body": {"ok": False, "error": "token_revoked"}}
    with pytest.raises(ReauthRequired):
        await fake_env.tokens.access_token(1, T)


async def test_admin_consent_denial_copy(app, fake_env):
    state = query(await fake_env.oauth.authorize_url(1, T))["state"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mavis.test") as c:
        r = await c.get("/oauth/testing/callback", params={"error": "consent_required", "state": state})
    assert r.status_code == 400 and "Testco needs an admin: https://idp.test/adminconsent" in r.text


async def test_internal_spec_is_hidden(fake_env):
    assert "slack_bot" not in [s.id for s in providers.user_providers()]
    with providers.registered(fp.make_spec(id="testing_int", internal=True, capabilities=(), anchor=None,
                                           pollers={}, toolkits=frozenset(), catalog=None)):
        assert "testing_int" not in [s.id for s in providers.user_providers()]
        assert providers.spec_of("testing_int") is not None


async def test_companion_grants_are_internal_specs(fake_env):
    providers.unregister("testing")
    bot = fp.make_spec(id="testing_bot", internal=True, capabilities=(), anchor=None, pollers={},
                       toolkits=frozenset(), catalog=None, action_scopes={}, capabilities_for=None)
    with providers.registered(bot), providers.registered(fp.make_spec(companions=("testing_bot",))):
        assert providers.spec_of("testing").companions == ("testing_bot",)
        assert [s.id for s in providers.user_providers() if s.id.startswith("testing")] == ["testing"]
