import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mavis.domain.policy import Capability as C
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.notion_spec import NOTION_SPEC

N = NativeProvider.NOTION
TOKEN = "https://api.notion.com/v1/oauth/token"
ME = "https://api.notion.com/v1/users/me"


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def notion_vendor(vendor, *, token=None, me=None):
    body = {"access_token": "ntn_access", "refresh_token": "ntn_refresh", "bot_id": "b1",
            "workspace_id": "w1", "workspace_name": "Acme\nHQ", "duplicated_template_id": "p1",
            "owner": {"type": "user", "user": {"id": "u1", "person": {"email": "Me@Acme.test"}}}}
    body.update(token or {})
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json=body)
    vendor.routes[ME] = lambda r: httpx.Response(200, json=me or {"id": "bot1", "type": "bot"})


def test_spec_is_registered_and_claims_notion():
    spec = providers.spec_of("notion")
    assert spec is NOTION_SPEC and spec.display_name == "Notion"
    assert spec.capabilities == (C.NOTION,) and spec.anchor is C.NOTION
    assert spec.token_auth == "basic_json" and spec.pkce is False and spec.revoke_url is None
    assert spec.gone_codes == frozenset({"invalid_grant", "unauthorized_client"})
    assert spec.forget_prefixes == ("notion:",) and spec.state_cursors == ("notion_",)
    assert spec.memory_sources == {"notion": ("notion:",)}
    assert spec.executor == ("notion", "NotionExecutor") and not spec.pollers
    assert spec.account_label({"workspace_name": "Acme"}) == "Acme"
    assert spec.external_label({"workspace_name": "Acme"}) == "the Acme workspace"


async def test_authorize_url_has_owner_user_and_no_scope(oauth):
    url = await oauth.authorize_url(1, N, None)
    assert url.startswith("https://api.notion.com/v1/oauth/authorize?")
    q = query(url)
    assert q["owner"] == "user" and q["response_type"] == "code" and q["client_id"] == "cid"
    assert "scope" not in q and "code_challenge" not in q


async def test_exchange_uses_basic_auth_and_json(oauth, vendor):
    notion_vendor(vendor)
    url = await oauth.authorize_url(1, N, None)
    redirect = query(url)["redirect_uri"]
    done = await oauth.complete(query(url)["state"], "code-1", N, None)
    req = vendor.to(TOKEN)[0]
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"cid:secret").decode()
    assert json.loads(req.content) == {"grant_type": "authorization_code", "code": "code-1",
                                       "redirect_uri": redirect}
    me = vendor.to(ME)[0]
    assert me.method == "GET" and me.headers["authorization"] == "Bearer ntn_access"
    assert me.headers["notion-version"] == "2026-03-11"
    assert done.account["bot_user_id"] == "bot1"


async def test_exchange_account_fields(oauth, vendor):
    notion_vendor(vendor)
    url = await oauth.authorize_url(1, N, None)
    done = await oauth.complete(query(url)["state"], "c", N, None)
    assert done.account == {
        "workspace_id": "w1", "workspace_name": "Acme HQ", "owner_id": "u1",
        "owner_email": "Me@Acme.test", "bot_user_id": "bot1", "template_page_id": "p1",
        "scopes": ["notion"]}
    assert NOTION_SPEC.account_key(done.account) == "w1/u1"


async def test_exchange_without_template_id_stores_empty_string(oauth, vendor):
    notion_vendor(vendor, token={"duplicated_template_id": None})
    url = await oauth.authorize_url(1, N, None)
    done = await oauth.complete(query(url)["state"], "c", N, None)
    assert done.account["template_page_id"] == ""


async def test_exchange_without_owner_email_stores_empty_string(oauth, vendor):
    notion_vendor(vendor, token={"owner": {"type": "user", "user": {"id": "u1"}}})
    url = await oauth.authorize_url(1, N, None)
    done = await oauth.complete(query(url)["state"], "c", N, None)
    assert done.account["owner_email"] == ""


@pytest.mark.parametrize("token", [
    {"access_token": None}, {"access_token": ""},
    {"owner": {"type": "workspace", "workspace": True}}, {"owner": None},
    {"owner": {"type": "user", "user": {}}}, {"workspace_id": ""}])
async def test_exchange_bad_shape_raises_exchange_failed(oauth, vendor, token):
    from mavis.tools.integrations.native.oauth import OAuthError

    notion_vendor(vendor, token=token)
    url = await oauth.authorize_url(1, N, None)
    with pytest.raises(OAuthError) as err:
        await oauth.complete(query(url)["state"], "c", N, None)
    assert err.value.kind == "exchange_failed"


async def test_exchange_fails_when_users_me_fails(oauth, vendor):
    from mavis.tools.integrations.native.oauth import OAuthError

    notion_vendor(vendor)
    vendor.routes[ME] = lambda r: httpx.Response(401, json={"code": "unauthorized"})
    url = await oauth.authorize_url(1, N, None)
    with pytest.raises(OAuthError):
        await oauth.complete(query(url)["state"], "c", N, None)


async def test_callback_saves_grant_and_lists_capability_notion(oauth, tokens, vendor):
    notion_vendor(vendor)
    url = await oauth.authorize_url(1, N, None)
    await oauth.complete(query(url)["state"], "c", N, None)
    grant = await tokens.grant(1, N)
    assert grant is not None and grant.account["workspace_id"] == "w1"
    assert NOTION_SPEC.caps_for(grant) == (C.NOTION,)
    assert await tokens.access_token(1, N) == "ntn_access"
