from __future__ import annotations

import base64
import json
from datetime import timedelta

import httpx
import pytest

from mavis.domain import timeutil
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.providers import ProviderSpec

T = NativeProvider.TESTING
URL = "https://idp.test/token"


def spec(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco", internal=True, token_url=URL,
                client_id_setting="notion_client_id", client_secret_setting="notion_client_secret",
                token_auth="basic_json")
    return ProviderSpec(**{**base, **kw})


async def test_basic_json_refresh_sends_basic_header_and_json_body(tokens, vendor, isolated_registry):
    isolated_registry.register(spec())
    await tokens.save(1, T, account={"scopes": []}, access_token="a-1", refresh_token="r1",
                      expires_at=timeutil.now() - timedelta(minutes=1))
    vendor.routes[URL] = lambda r: httpx.Response(200, json={"access_token": "a-2", "refresh_token": "r2"})
    assert await tokens.access_token(1, T) == "a-2"
    (req,) = vendor.to(URL)
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"cid:secret").decode()
    assert json.loads(req.content) == {"grant_type": "refresh_token", "refresh_token": "r1"}
    assert b"client_secret" not in req.content
    assert "application/json" in req.headers["content-type"]
    vendor.routes[URL] = lambda r: httpx.Response(200, json={"access_token": "a-3"})
    await tokens.access_token(1, T, force=True)
    assert json.loads(vendor.to(URL)[-1].content)["refresh_token"] == "r2"


async def test_body_auth_stays_the_default(tokens, vendor, isolated_registry):
    isolated_registry.register(spec(token_auth="body"))
    await tokens.save(1, T, account={"scopes": []}, access_token="a-1", refresh_token="r1",
                      expires_at=timeutil.now() - timedelta(minutes=1))
    vendor.routes[URL] = lambda r: httpx.Response(200, json={"access_token": "a-2"})
    await tokens.access_token(1, T)
    (req,) = vendor.to(URL)
    assert "authorization" not in req.headers and b"client_secret=secret" in req.content


def test_unknown_token_auth_is_rejected_at_register(isolated_registry):
    with pytest.raises(ValueError, match="token_auth"):
        isolated_registry.register(spec(token_auth="x"))
