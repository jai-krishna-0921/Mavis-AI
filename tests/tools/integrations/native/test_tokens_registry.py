from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from mavis.domain import timeutil
from mavis.domain.errors import IntegrationError
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.providers import ProviderSpec
from mavis.tools.integrations.native.tokens import REVOKED, account_key

from .conftest import form

T = NativeProvider.TESTING
URL = "https://idp.test/token"


def fake(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco", internal=True, token_url=URL,
                client_id_setting="slack_client_id", client_secret_setting="slack_client_secret",
                gone_codes=frozenset({"interaction_required"}),
                refresh_params={"scope": "mail.read offline_access"},
                account_key=lambda a: f"{a['tid']}/{a['oid']}")
    return ProviderSpec(**{**base, **kw})


async def seed(tokens):
    await tokens.save(1, T, account={"tid": "t1", "oid": "o1", "scopes": ["mail.read"]}, access_token="a-1",
                      refresh_token="r-1", expires_at=timeutil.now() - timedelta(minutes=1))


async def test_account_key_comes_from_the_spec(isolated_registry):
    isolated_registry.register(fake())
    assert account_key(T, {"tid": "t1", "oid": "o1"}) == "t1/o1"
    assert account_key(NativeProvider.GOOGLE, {"email": "A@B.com"}) == "a@b.com"


async def test_refresh_uses_spec_url_credentials_and_extra_params(tokens, vendor, isolated_registry):
    isolated_registry.register(fake())
    await seed(tokens)
    sent = []
    vendor.routes[URL] = lambda r: (sent.append(form(r)),
                                    httpx.Response(200, json={"access_token": "a-2", "refresh_token": "r-2",
                                                              "expires_in": 3600}))[1]
    assert await tokens.access_token(1, T) == "a-2"
    assert sent[0]["scope"] == "mail.read offline_access" and sent[0]["client_id"] == "sid"
    assert sent[0]["client_secret"] == "ssecret" and sent[0]["grant_type"] == "refresh_token"
    assert (await tokens.owner_of_key(T, "t1/o1")) == 1
    assert (await tokens.owner_of_key(T, "t1/zz")) is None


async def test_spec_gone_codes_end_the_grant(tokens, vendor, isolated_registry):
    isolated_registry.register(fake())
    await seed(tokens)
    vendor.routes[URL] = lambda r: httpx.Response(400, json={"error": "interaction_required"})
    with pytest.raises(ReauthRequired):
        await tokens.access_token(1, T)
    assert (await tokens.grant(1, T)).status == REVOKED


async def test_unlisted_error_is_not_gone(tokens, vendor, isolated_registry):
    isolated_registry.register(fake())
    await seed(tokens)
    vendor.routes[URL] = lambda r: httpx.Response(400, json={"error": "temporarily_unavailable"})
    with pytest.raises(IntegrationError):
        await tokens.access_token(1, T)
    assert (await tokens.grant(1, T)).status == "ACTIVE"


async def test_soft_errors_flag_reads_ok_false(tokens, vendor, isolated_registry):
    isolated_registry.register(fake(soft_errors=True, gone_codes=frozenset({"token_revoked"})))
    await seed(tokens)
    vendor.routes[URL] = lambda r: httpx.Response(200, json={"ok": False, "error": "token_revoked"})
    with pytest.raises(ReauthRequired):
        await tokens.access_token(1, T)
