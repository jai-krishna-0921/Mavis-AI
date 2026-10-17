from __future__ import annotations

from mavis.domain.policy import Capability
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.providers import CatalogEntry, ProviderSpec
from tests.api.dash_helpers import *  # noqa: F403 - fixtures and helpers
from tests.api.dash_helpers import query, signed_in

API = "/api/v1"

T = NativeProvider.TESTING


def fake(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco", auth_url="https://idp.test/a",
                token_url="https://idp.test/t", client_id_setting="slack_client_id",
                client_secret_setting="slack_client_secret", account_key=lambda a: a.get("k"),
                account_label=lambda a: a.get("mail"),
                capabilities=(Capability.NOTION, Capability.SANDBOX), anchor=Capability.NOTION,
                memory_sources={"tco": ("tco:",)},
                catalog=CatalogEntry(description="Notes and boxes.",
                                     services={"notes": frozenset({"n.read"}),
                                               "boxes": frozenset({"b.read"})},
                                     icon="testco", name="Testco Suite", chips=True))
    return ProviderSpec(**{**base, **kw})


async def test_registered_provider_appears_with_icon_services_and_label(app, tokens, isolated_registry):
    isolated_registry.register(fake())
    c, _ = await signed_in(app, 1)
    async with c:
        rows = (await c.get(f"{API}/connectors")).json()
        assert [r["id"] for r in rows] == ["google", "slack", "microsoft", "testing"]
        testco = rows[3]
        assert (testco["name"], testco["icon"], testco["services"], testco["status"]) == (
            "Testco Suite", "testco", ["notes", "boxes"], "none")
        await tokens.save(1, T, account={"k": "k1", "mail": "me@corp.test", "scopes": ["n.read"]},
                          access_token="a", refresh_token="r", expires_at=None)
        testco = (await c.get(f"{API}/connectors")).json()[3]
        assert testco["status"] == "active" and testco["account"] == "me@corp.test"
        assert testco["scopes_granted"] == ["notes"] and testco["missing_scopes"] == ["boxes"]


async def test_builtin_rows_carry_icon_and_chip_names(app):
    c, _ = await signed_in(app, 1)
    async with c:
        google, slack, *_rest = (await c.get(f"{API}/connectors")).json()
        assert google["icon"] == "google" and google["name"] == "Google Workspace"
        assert google["services"] == ["mail", "calendar", "drive", "docs", "sheets", "tasks", "meet",
                                      "contacts"]
        assert slack["icon"] == "slack" and slack["name"] == "Slack" and slack["services"] == []


async def test_unconfigured_provider_is_hidden_unless_always_listed(app, isolated_registry):
    isolated_registry.register(fake(client_id_setting="no_such_setting"))
    c, _ = await signed_in(app, 1)
    async with c:
        rows = (await c.get(f"{API}/connectors")).json()
        assert [r["id"] for r in rows] == ["google", "slack", "microsoft"]


async def test_connect_uses_the_spec_url_and_unknown_is_404(app, oauth, isolated_registry):
    isolated_registry.register(fake())
    c, csrf = await signed_in(app, 1)
    async with c:
        url = (await c.post(f"{API}/connectors/testing/connect", headers=csrf, json={})).json()["url"]
        assert url.startswith("https://idp.test/a?") and query(url)["client_id"] == "sid"
        assert (await c.post(f"{API}/connectors/slack_bot/connect", headers=csrf, json={})).status_code == 404
        assert (await c.post(f"{API}/connectors/nope/connect", headers=csrf, json={})).status_code == 404
