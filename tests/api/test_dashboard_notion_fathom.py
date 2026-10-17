from __future__ import annotations

from mavis.tools.integrations.native.base import NativeProvider
from tests.api.dash_helpers import *  # noqa: F403 - fixtures and helpers
from tests.api.dash_helpers import signed_in

API = "/api/v1"
N = NativeProvider.NOTION


async def _rows(c) -> dict[str, dict]:
    """Rows by id: the order around them depends on which providers are registered."""
    return {r["id"]: r for r in (await c.get(f"{API}/connectors")).json()}


def _configure(monkeypatch, **env) -> None:
    from mavis.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


async def test_notion_and_fathom_cards_listed_only_when_configured(app, monkeypatch):
    _configure(monkeypatch, NOTION_CLIENT_ID="", NOTION_CLIENT_SECRET="", FATHOM_CLIENT_ID="",
               FATHOM_CLIENT_SECRET="")
    c, _ = await signed_in(app, 1)
    async with c:
        assert not {"notion", "fathom"} & set(await _rows(c))
        _configure(monkeypatch, NOTION_CLIENT_ID="nid", NOTION_CLIENT_SECRET="ns")
        rows = await _rows(c)
        assert "notion" in rows and "fathom" not in rows
        _configure(monkeypatch, FATHOM_CLIENT_ID="fid", FATHOM_CLIENT_SECRET="fs")
        rows = await _rows(c)
        notion, fathom = rows["notion"], rows["fathom"]
        assert (notion["name"], notion["icon"], notion["status"]) == ("Notion", "notion", "none")
        assert (fathom["name"], fathom["icon"], fathom["status"]) == ("Fathom", "fathom", "none")
        assert notion["services"] == [] and fathom["services"] == []  # one service each: no chips
        # the hook companion is internal: it never gets a card
        assert "fathom_hook" not in rows


async def test_card_account_label_is_workspace_name(app, tokens, monkeypatch):
    _configure(monkeypatch, NOTION_CLIENT_ID="nid", NOTION_CLIENT_SECRET="ns")
    c, _ = await signed_in(app, 1)
    async with c:
        await tokens.save(1, N, account={"workspace_id": "w1", "workspace_name": "Orbit HQ", "owner_id": "o1",
                                         "scopes": ["notion"]},
                          access_token="a", refresh_token="r", expires_at=None)
        card = (await _rows(c))["notion"]
        assert card["status"] == "active" and card["account"] == "Orbit HQ"
        assert card["scopes_granted"] == ["notion"] and card["missing_scopes"] == []
        assert "o1" not in str(card) and "w1" not in str(card)


async def test_disconnect_route_forgets_notion_memory(app, tokens, monkeypatch):
    _configure(monkeypatch, NOTION_CLIENT_ID="nid", NOTION_CLIENT_SECRET="ns")
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
        await tokens.save(1, N, account={"workspace_id": "w1", "workspace_name": "Orbit HQ", "owner_id": "o1",
                                         "scopes": ["notion"]},
                          access_token="a", refresh_token="r", expires_at=None)
        r = await c.post(f"{API}/connectors/notion/disconnect", headers=csrf, json={"forget": True})
        assert r.status_code == 204
    assert forgotten == ["notion"]
