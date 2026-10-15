import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mavis.domain.policy import Capability as C
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.microsoft_spec import (
    MS_SCOPES,
    MS_TOKEN_URL,
    PERSONAL_TENANT,
    admin_link,
    denial_kind,
)

from .conftest import form

M = NativeProvider.MICROSOFT
TID, OID = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def id_token(**claims) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    body = {"aud": "mid", "tid": TID, "oid": OID, "preferred_username": "Me@Orbit.test", **claims}
    return f"{b64({'alg': 'RS256'})}.{b64(body)}.sig"


def ms_vendor(vendor, *, scope="openid profile email User.Read Mail.ReadWrite Mail.Send Calendars.ReadWrite "
                                "Chat.ReadWrite ChatMessage.Send Files.Read.All", **claims):
    vendor.routes[MS_TOKEN_URL] = lambda r: httpx.Response(200, json={
        "access_token": "eyJ.access", "refresh_token": "0.AAA-r1", "expires_in": 3599, "scope": scope,
        "id_token": id_token(**claims)})
    vendor.routes["https://graph.microsoft.com/v1.0/me"] = lambda r: httpx.Response(
        200, json={"mail": "Me@Orbit.test", "userPrincipalName": "me@orbit.onmicrosoft.com"})


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_spec_is_registered_with_the_four_capabilities():
    spec = providers.spec_of("microsoft")
    assert spec is not None and spec.display_name == "Microsoft 365"
    assert spec.capabilities == (C.OUTLOOK, C.OUTLOOK_CALENDAR, C.TEAMS, C.ONEDRIVE)
    assert spec.anchor is C.OUTLOOK and spec.pkce is True
    assert "invalid_grant" in spec.gone_codes and "interaction_required" in spec.gone_codes
    assert {"microsoft", "outlook", "teams", "onedrive", "outlookcalendar"} <= spec.toolkits
    assert spec.forget_prefixes == ("outlook:", "ocal:", "teams:", "onedrive:")
    assert len(MS_SCOPES) == 11 and "ChannelMessage.Read.All" not in MS_SCOPES


async def test_authorize_url_uses_common_authority_pkce_and_scopes(oauth):
    url = await oauth.authorize_url(1, M, 7)
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    q = query(url)
    assert q["client_id"] == "mid" and q["response_type"] == "code"
    assert q["redirect_uri"] == "https://mavis.test/oauth/microsoft/callback"
    assert q["code_challenge_method"] == "S256" and q["response_mode"] == "query"
    assert q["prompt"] == "select_account"
    assert set(q["scope"].split()) == set(MS_SCOPES)


async def test_exchange_takes_identity_from_the_id_token(oauth, vendor):
    ms_vendor(vendor)
    url = await oauth.authorize_url(1, M, None)
    done = await oauth.complete(query(url)["state"], "code-1", M, None)
    sent = form(vendor.to(MS_TOKEN_URL)[0])
    assert sent["grant_type"] == "authorization_code" and sent["client_secret"] == "msecret"
    assert sent["code_verifier"] and sent["scope"] == " ".join(MS_SCOPES)
    assert done.account["tid"] == TID and done.account["oid"] == OID
    assert done.account["mail"] == "me@orbit.test"
    assert "User.Read" in done.account["scopes"] and "Mail.ReadWrite" in done.account["scopes"]
    spec = providers.spec_of("microsoft")
    assert spec.account_key(done.account) == f"{TID}/{OID}"
    assert spec.account_label(done.account) == "me@orbit.test"


async def test_scope_urls_from_graph_are_normalised(oauth, vendor):
    ms_vendor(vendor, scope="https://graph.microsoft.com/Mail.ReadWrite "
                            "https://graph.microsoft.com/User.Read openid")
    done = await oauth.complete(query(await oauth.authorize_url(1, M, None))["state"], "c", M, None)
    assert {"Mail.ReadWrite", "User.Read", "openid"} <= set(done.account["scopes"])


async def test_id_token_for_another_app_is_refused(oauth, vendor):
    ms_vendor(vendor, aud="someone-else")
    from mavis.tools.integrations.native.oauth import OAuthError
    with pytest.raises(OAuthError) as e:
        await oauth.complete(query(await oauth.authorize_url(1, M, None))["state"], "c", M, None)
    assert e.value.kind == "exchange_failed"


async def test_consumer_account_without_oid_falls_back_to_sub(oauth, vendor):
    ms_vendor(vendor, tid=PERSONAL_TENANT, oid=None, sub="S-1")
    done = await oauth.complete(query(await oauth.authorize_url(1, M, None))["state"], "c", M, None)
    assert providers.spec_of("microsoft").account_key(done.account) == f"{PERSONAL_TENANT}/S-1"


async def test_personal_account_drops_teams_and_a_missing_scope_drops_its_capability(tokens):
    spec = providers.spec_of("microsoft")
    await tokens.save(1, M, account={"tid": PERSONAL_TENANT, "oid": "o", "mail": "p@outlook.com",
                                     "scopes": list(MS_SCOPES)}, access_token="a", refresh_token="r",
                      expires_at=None)
    assert spec.caps_for(await tokens.grant(1, M)) == (C.OUTLOOK, C.OUTLOOK_CALENDAR, C.ONEDRIVE)
    await tokens.save(2, M, account={"tid": TID, "oid": "o2", "mail": "w@corp.test",
                                     "scopes": [s for s in MS_SCOPES if s != "Chat.ReadWrite"]},
                      access_token="a", refresh_token="r", expires_at=None)
    assert C.TEAMS not in spec.caps_for(await tokens.grant(2, M))
    await tokens.save(3, M, account={"tid": TID, "oid": "o3", "mail": "w3@corp.test",
                                     "scopes": list(MS_SCOPES)}, access_token="a", refresh_token="r",
                      expires_at=None)
    assert len(spec.caps_for(await tokens.grant(3, M))) == 4


@pytest.mark.parametrize("error,desc,kind", [
    ("access_denied", "AADSTS65001: The user or administrator has not consented", "admin_consent"),
    ("invalid_request", "AADSTS90094: The grant requires admin permission", "admin_consent"),
    ("invalid_request", "AADSTS90095: Admin consent is required", "admin_consent"),
    ("access_denied", "AADSTS50105: user not assigned to a role", "admin_consent"),
    ("consent_required", None, "admin_consent"),
    ("admin_consent_required", None, "admin_consent"),
    ("access_denied", "AADSTS65004: The user declined to consent", "denied"),
    ("access_denied", None, "denied"),
    (None, None, "denied"),
])
def test_denial_kinds(error, desc, kind):
    assert denial_kind(error, desc) == kind


def test_admin_link_names_the_app_and_the_callback(native_env):
    q = query(admin_link())
    assert admin_link().startswith("https://login.microsoftonline.com/organizations/v2.0/adminconsent?")
    assert q["client_id"] == "mid" and q["scope"] == "https://graph.microsoft.com/.default"
    assert q["redirect_uri"] == "https://mavis.test/oauth/microsoft/callback"


def test_admin_text_carries_the_link_and_no_vendor_words():
    text = providers.spec_of("microsoft").user_text["admin_consent"]
    assert "{admin_link}" in text and "AADSTS" not in text and "\u2014" not in text and "\u2013" not in text


async def test_a_failing_key_derivation_is_an_exchange_failure(oauth, vendor, monkeypatch):
    from dataclasses import replace

    from mavis.tools.integrations.native.oauth import OAuthError

    ms_vendor(vendor)
    spec = providers.spec_of("microsoft")
    monkeypatch.setitem(providers.PROVIDERS, "microsoft", replace(spec, account_key=lambda a: a["nope"]))
    with pytest.raises(OAuthError) as e:
        await oauth.complete(query(await oauth.authorize_url(1, M, None))["state"], "c", M, None)
    assert e.value.kind == "exchange_failed" and e.value.user_id == 1
    assert "nope" not in str(e.value)
    assert await oauth.tokens.grant(1, M) is None

