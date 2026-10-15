# tests/tools/integrations/native/fake_provider.py
"""A third native provider that exists only in tests: every ProviderSpec field set, vendor routes scripted."""

from __future__ import annotations

from datetime import timedelta

import httpx

from mavis.domain import timeutil
from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability
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
    body = await req.post(TOKEN, data={"code": req.code, "code_verifier": req.verifier,
                                       "client_id": req.client_id})
    if not body.get("access_token"):
        raise ExchangeFailed
    account = {"tid": "t1", "oid": "o1", "mail": "me@corp.test", "plan": "work", "scopes": ["mail.read"]}
    return Exchange(account, body["access_token"], body.get("refresh_token"), None)


async def revoke(req: RevokeRequest) -> None:
    CALLS.append(("revoke", req.access_token))
    await req.post("https://idp.test/revoke", data={"token": req.access_token})


async def after_save(a) -> None:
    CALLS.append(("after_save", a.user_id))


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
        client_id_setting="slack_client_id", client_secret_setting="slack_client_secret",
        refresh_params={"scope": "mail.read offline_access"},
        gone_codes=frozenset({"interaction_required"}),
        exchange=exchange, revoke=revoke, after_save=after_save,
        family_enabled=lambda: True, denial_kind=denial, admin_link=lambda: "https://idp.test/adminconsent",
        account_key=lambda a: f"{a['tid']}/{a['oid']}", account_label=lambda a: a.get("mail"),
        external_label=lambda a: a["mail"], remember_identity=remember,
        capabilities=(Capability.NOTION, Capability.SANDBOX), anchor=Capability.NOTION,
        capabilities_for=personal_trim, action_scopes={"notion.search": (NEED,), "sandbox.run": (NEED,)},
        toolkits=frozenset({"testco", "notion", "sandbox"}),
        features=(("search notes", "notion.search"),), abilities="notes and boxes",
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


_CB = "test_connect_url_and_callback_page"
_REF = "test_refresh_gone_codes_and_params"
_DEL = "test_disconnect_forgets_and_deletion_revokes"
_RTR = "test_status_and_execute_through_the_router"
_DASH = "test_dashboard_card_and_connect"
_DENY = "test_admin_consent_denial_copy"

COVERAGE = {
    "id": _CB,
    "display_name": _CB,
    "brand": _CB,
    "internal": "test_internal_spec_is_hidden",
    "auth_url": _CB,
    "token_url": _REF,
    "revoke_url": _DEL,
    "scope_params": _CB,
    "pkce": _CB,
    "extra_auth_params": _CB,
    "client_id_setting": _CB,
    "client_secret_setting": _REF,
    "refresh_params": _REF,
    "gone_codes": _REF,
    "soft_errors": _REF,
    "exchange": _CB,
    "revoke": _DEL,
    "after_save": _CB,
    "companions": "test_companion_grants_are_internal_specs",
    "denial_kind": _DENY,
    "admin_link": _DENY,
    "account_key": _CB,
    "account_label": _DASH,
    "external_label": _CB,
    "remember_identity": _CB,
    "capabilities": _RTR,
    "anchor": _CB,
    "family_enabled": _RTR,
    "capabilities_for": "test_capabilities_for_trims_a_personal_grant",
    "action_scopes": _RTR,
    "toolkits": _DEL,
    "executor": _RTR,
    "features": _RTR,
    "abilities": _DEL,
    "reconnect_hint": _DEL,
    "nothing_allowed": _RTR,
    "pollers": "test_poll_hook_runs",
    "legacy_capabilities": _DEL,
    "family_label": _DEL,
    "forget_prefixes": _DEL,
    "memory_sources": _DEL,
    "catalog": _DASH,
    "user_text": _DENY,
}
