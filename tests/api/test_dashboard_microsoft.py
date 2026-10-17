from __future__ import annotations

from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.microsoft_spec import MS_SCOPES, PERSONAL_TENANT
from tests.api.dash_helpers import *  # noqa: F403 - fixtures and helpers
from tests.api.dash_helpers import query, signed_in

API = "/api/v1"
M = NativeProvider.MICROSOFT


def _acct(tid: str, mail: str, scopes=MS_SCOPES) -> dict:
    return {"tid": tid, "oid": "o1", "mail": mail, "scopes": list(scopes)}


async def test_hidden_until_configured(app, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "")
    get_settings.cache_clear()
    c, _ = await signed_in(app, 1)
    async with c:
        assert [r["id"] for r in (await c.get(f"{API}/connectors")).json()] == ["google", "slack", "notion"]


async def test_listed_when_configured_with_icon_chips_and_admin_link(app):
    c, _ = await signed_in(app, 1)
    async with c:
        ms = (await c.get(f"{API}/connectors")).json()[2]
        assert ms["id"] == "microsoft" and ms["name"] == "Microsoft 365" and ms["icon"] == "microsoft"
        assert ms["services"] == ["outlook", "calendar", "teams", "onedrive"] and ms["status"] == "none"
        link = ms["admin_link"]
        assert link.startswith("https://login.microsoftonline.com/organizations/v2.0/adminconsent?")
        assert query(link)["client_id"] == "mid"
        assert ms["note"] is None


async def test_connected_work_and_personal_accounts(app, tokens):
    c, _ = await signed_in(app, 1)
    async with c:
        await tokens.save(1, M, account=_acct("11111111-2222-3333-4444-555555555555", "me@corp.test"),
                          access_token="a", refresh_token="r", expires_at=None)
        ms = (await c.get(f"{API}/connectors")).json()[2]
        assert ms["status"] == "active" and ms["account"] == "me@corp.test" and ms["note"] is None
        assert ms["unavailable"] == [] and "teams" in ms["scopes_granted"]
        assert ms["missing_scopes"] == []
        await tokens.save(1, M, account=_acct(PERSONAL_TENANT, "me@outlook.com"),
                          access_token="a", refresh_token="r", expires_at=None)
        ms = (await c.get(f"{API}/connectors")).json()[2]
        assert ms["account"] == "me@outlook.com"
        assert ms["note"] == "Teams is not available for personal Microsoft accounts."
        # the chip must not read "allowed" next to that note, and it is not "missing" either
        assert "teams" not in ms["scopes_granted"] and "teams" not in ms["missing_scopes"]
        assert ms["unavailable"] == ["teams"] and "outlook" in ms["scopes_granted"]


async def test_work_account_without_chat_scope_gets_the_teams_note(app, tokens):
    c, _ = await signed_in(app, 1)
    async with c:
        scopes = [s for s in MS_SCOPES if s != "Chat.ReadWrite"]
        await tokens.save(1, M, account=_acct("11111111-2222-3333-4444-555555555555", "me@corp.test", scopes),
                          access_token="a", refresh_token="r", expires_at=None)
        ms = (await c.get(f"{API}/connectors")).json()[2]
        assert ms["note"] == "Teams was not allowed on this account." and ms["missing_scopes"] == ["teams"]


async def test_connect_returns_the_microsoft_authorize_url(app, oauth):
    c, csrf = await signed_in(app, 1)
    async with c:
        r = await c.post(f"{API}/connectors/microsoft/connect", headers=csrf, json={})
        url = r.json()["url"]
        assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
        assert query(url)["redirect_uri"].endswith("/oauth/microsoft/callback")


async def test_disconnect_forgets_all_four_sources(app, tokens, monkeypatch):
    forgotten = []

    class Vault:
        async def forget_source(self, uid, source):
            forgotten.append(source)

    class Flow:
        async def disconnect(self, uid, anchor, forget=True):
            return True

    monkeypatch.setattr("mavis.api.dashboard.connectors.get_vault", lambda: Vault())
    monkeypatch.setattr("mavis.tools.integrations.wiring.get_connect_flow", lambda: Flow())
    c, csrf = await signed_in(app, 1)
    async with c:
        await tokens.save(1, M, account=_acct("11111111-2222-3333-4444-555555555555", "m@corp.test"),
                          access_token="a", refresh_token="r", expires_at=None)
        r = await c.post(f"{API}/connectors/microsoft/disconnect", headers=csrf, json={"forget": True})
        assert r.status_code == 204
    assert set(forgotten) == {"outlook", "outlook_calendar", "teams", "onedrive"}
