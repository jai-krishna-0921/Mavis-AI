# tests/tools/integrations/native/fake_provider.py
"""A third native provider that exists only in tests: every ProviderSpec field set, vendor routes scripted."""

from __future__ import annotations

from datetime import timedelta

import httpx

from mavis.domain import timeutil
from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.providers import (
    CatalogEntry,
    Exchange,
    ExchangeFailed,
    ExchangeRequest,
    ProviderSpec,
    RevokeRequest,
    ScopeParam,
)

T = NativeProvider.TESTING
CALLS: list[tuple] = []
NEED = frozenset({"mail.read"})
TOKEN = "https://idp.test/token"


async def exchange(req: ExchangeRequest) -> Exchange:
    url = providers.spec_of("testing").token_url
    form = {"code": req.code, "code_verifier": req.verifier, "client_id": req.client_id}
    body = await req.post(url, data=form)
    if not body.get("access_token"):
        raise ExchangeFailed
    account = {"tid": "t1", "oid": "o1", "mail": "me@corp.test", "plan": "work", "scopes": ["mail.read"]}
    return Exchange(account, body["access_token"], body.get("refresh_token"), None, {"bot": "b1"})


async def revoke(req: RevokeRequest) -> None:
    CALLS.append(("revoke", req.access_token))
    await req.post(providers.spec_of("testing").revoke_url, data={"token": req.access_token})


async def after_save(a) -> None:
    CALLS.append(("after_save", a.user_id))
    for companion in providers.spec_of("testing").companions:  # the Slack bot pattern: a second grant
        await a.tokens.save(a.user_id, NativeProvider(companion), account={"team_id": "T1", "scopes": []},
                            access_token=a.exchange.extras["bot"], refresh_token=None, expires_at=None)


async def remember(user_id: int, account: dict) -> None:
    CALLS.append(("remember", user_id, account["mail"]))


async def poll(poller, user_id: int, st: dict) -> int:
    await poller.set_cursor(user_id, st, "fake", "1")
    return 1


def denial(error, description) -> str:
    return "admin_consent" if error == "consent_required" else "denied"


def personal_trim(grant) -> tuple[Capability, ...]:
    if grant.account.get("plan") == "personal":
        return (Capability.NOTION,)
    return (Capability.NOTION, Capability.SANDBOX)


def make_spec(**kw) -> ProviderSpec:
    base = dict(
        id="testing", display_name="Testco", brand="Testco",
        auth_url="https://idp.test/authorize", token_url=TOKEN, revoke_url="https://idp.test/revoke",
        scope_params=(ScopeParam("scope", ("openid", "mail.read"), " "),), pkce=True,
        extra_auth_params={"prompt": "select_account"},
        client_id_setting="testing_client_id", client_secret_setting="testing_client_secret",
        refresh_params={"scope": "mail.read offline_access"},
        gone_codes=frozenset({"interaction_required"}),
        exchange=exchange, revoke=revoke, after_save=after_save,
        family_enabled=lambda: True, denial_kind=denial, admin_link=lambda: "https://idp.test/adminconsent",
        account_key=lambda a: f"{a['tid']}/{a['oid']}", account_label=lambda a: a.get("mail"),
        external_label=lambda a: a["mail"], remember_identity=remember,
        capabilities=(Capability.NOTION, Capability.SANDBOX), anchor=Capability.NOTION,
        capabilities_for=personal_trim, action_scopes={"notion.search": (NEED,), "sandbox.run": (NEED,)},
        toolkits=frozenset({"testco", "notion", "sandbox"}),
        features=(("search notes", "notion.search"), ("run boxes", "sandbox.run")),
        abilities="notes and boxes",
        reconnect_hint="send /connect testing",
        nothing_allowed="You signed in with {account} but allowed nothing.",
        pollers={Capability.SANDBOX: "tests.tools.integrations.native.fake_provider:poll"},
        forget_prefixes=("tco:",), memory_sources={"tco": ("tco:",)}, family_label="Testco Suite",
        catalog=CatalogEntry(description="Notes and boxes.", services={"notes": NEED}, icon="testco",
                             name="Testco Suite", chips=True, always_listed=False),
        user_text={"admin_consent": "{name} needs an admin: {admin_link}"},
    )
    return ProviderSpec(**{**base, **kw})


class FakeExec:
    provider = T

    def handles(self, action: str) -> bool:
        return action == "notion.search"

    async def execute(self, user, action, args):
        return ToolResult(ok=True, data={"native": action})


def use_settings(monkeypatch) -> None:
    """The fake's own client id and secret (Settings has no such fields: set them on the class)."""
    from mavis.config import Settings

    monkeypatch.setattr(Settings, "testing_client_id", "tid", raising=False)
    monkeypatch.setattr(Settings, "testing_client_secret", "tsecret", raising=False)


def add_actions(monkeypatch) -> None:
    """The action table has no SANDBOX action; `covers` needs one for the fake's second capability."""
    from pydantic import BaseModel

    from mavis.domain.policy import RiskClass
    from mavis.tools.integrations import actions

    spec = actions.ActionSpec("sandbox.run", Capability.SANDBOX, "Run in the test sandbox.", BaseModel,
                              RiskClass.READ, frozenset({"conversation"}))
    monkeypatch.setitem(actions.ACTIONS, "sandbox.run", spec)


def install(vendor) -> None:
    vendor.fail = None

    def token(request):
        if vendor.fail:
            return httpx.Response(vendor.fail["status"], json=vendor.fail["body"])
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})

    vendor.routes[TOKEN] = token
    vendor.routes["https://idp.test/revoke"] = lambda r: httpx.Response(200, json={})


async def grant(tokens, *, user: int = 1, plan: str = "work", expired: bool = False) -> None:
    exp = timeutil.now() + (timedelta(minutes=-1) if expired else timedelta(hours=1))
    await tokens.save(user, T, account={"tid": "t1", "oid": f"o{user}", "mail": "me@corp.test", "plan": plan,
                                        "scopes": ["mail.read"]},
                      access_token="a", refresh_token="r", expires_at=exp)


# Field -> "path::test" that proves a consumer reads it. The guard in test_provider_fields_e2e.py resolves
# every entry, so renaming or deleting a probe anywhere fails the suite.
_E = "tests/api/test_provider_fields_e2e.py::"
COVERAGE = {
    "id": _E + "test_connect_url_and_callback_page",
    "display_name": _E + "test_connect_url_and_callback_page",
    "brand": _E + "test_connect_url_and_callback_page",
    "internal": _E + "test_internal_spec_has_no_routes_card_or_listing",
    "auth_url": _E + "test_connect_url_and_callback_page",
    "token_url": _E + "test_refresh_sends_the_specs_params_and_credentials",
    "revoke_url": _E + "test_revoke_goes_to_the_specs_revoke_url",
    "scope_params": _E + "test_connect_url_and_callback_page",
    "pkce": _E + "test_connect_url_and_callback_page",
    "extra_auth_params": _E + "test_connect_url_and_callback_page",
    "client_id_setting": _E + "test_connect_url_and_callback_page",
    "client_secret_setting": _E + "test_refresh_sends_the_specs_params_and_credentials",
    "refresh_params": _E + "test_refresh_sends_the_specs_params_and_credentials",
    "gone_codes": _E + "test_gone_codes_end_the_grant",
    "soft_errors": _E + "test_soft_errors_override_reads_a_200_error_body",
    "exchange": _E + "test_connect_url_and_callback_page",
    "revoke": _E + "test_disconnect_forgets_the_prefixes_and_tells_the_vendor",
    "after_save": _E + "test_companion_grant_is_saved_on_connect_and_deleted_on_revoke",
    "companions": _E + "test_companion_grant_is_saved_on_connect_and_deleted_on_revoke",
    "denial_kind": _E + "test_admin_consent_denial_copy",
    "admin_link": _E + "test_admin_consent_denial_copy",
    "account_key": _E + "test_connect_url_and_callback_page",
    "account_label": _E + "test_dashboard_card_and_connect",
    "external_label": _E + "test_nothing_allowed_copy_and_revoke",
    "remember_identity": _E + "test_connect_url_and_callback_page",
    "capabilities": _E + "test_status_and_execute_through_the_router",
    "anchor": _E + "test_connect_url_and_callback_page",
    "family_enabled": _E + "test_family_gate_off_keeps_capabilities_apart",
    "legacy_capabilities": _E + "test_google_only_hooks_follow_legacy_capabilities",
    "family_label": _E + "test_menu_and_status_use_the_family_label",
    "capabilities_for": _E + "test_capabilities_for_trims_a_personal_grant",
    "action_scopes": _E + "test_status_and_execute_through_the_router",
    "toolkits": _E + "test_connect_url_and_callback_page",
    "features": _E + "test_features_split_into_allowed_and_not",
    "abilities": _E + "test_announcement_uses_abilities_and_the_reconnect_hint",
    "reconnect_hint": _E + "test_announcement_uses_abilities_and_the_reconnect_hint",
    "nothing_allowed": _E + "test_nothing_allowed_copy_and_revoke",
    "pollers": _E + "test_poll_hook_runs",
    "forget_prefixes": _E + "test_disconnect_forgets_the_prefixes_and_tells_the_vendor",
    "memory_sources": _E + "test_dashboard_disconnect_forgets_exactly_the_memory_sources",
    "catalog": _E + "test_dashboard_card_and_connect",
    "user_text": _E + "test_admin_consent_denial_copy",
    "executor": "tests/tools/integrations/native/test_router.py::"
                "test_load_executors_skips_absent_modules_but_not_broken_ones",
}
