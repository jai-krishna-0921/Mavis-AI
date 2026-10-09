"""Connectors: Google and Slack through the native OAuth. Reuses the router's grants, scope tables and
ConnectFlow; the browser is sent to the same consent screen Telegram's /connect uses."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from mavis.api.dashboard import common
from mavis.api.dashboard.common import DashError
from mavis.domain.policy import Capability
from mavis.memory.vault import VaultError, get_vault
from mavis.tools.integrations.native import oauth as oauth_mod
from mavis.tools.integrations.native import router as native_router
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from mavis.web.sessions import Active

router = APIRouter(prefix="/connectors")
log = structlog.get_logger(__name__)

WEB_ORIGIN = "web"

# Google services shown in the UI and the scope groups that count as having each one (the router's own
# groups, so the page agrees with what Mavis can actually do).
GOOGLE_SERVICES: dict[str, frozenset[str]] = {
    "mail": native_router._MAIL_READ, "calendar": native_router._CAL_READ,
    "drive": native_router._DRIVE_META, "docs": native_router._DOCS_READ,
    "sheets": native_router._SHEETS_READ, "tasks": native_router._TASKS_READ,
    "meet": native_router._MEET_CREATE, "contacts": native_router._CONTACTS_READ,
}
SLACK_SERVICES: dict[str, frozenset[str]] = {
    "messages": frozenset({"channels:history", "groups:history", "im:history", "mpim:history"}),
    "channels": frozenset({"channels:read", "groups:read", "im:read", "mpim:read"}),
    "people": frozenset({"users:read"}), "search": frozenset({"search:read"}),
    "post": frozenset({"chat:write"}),
}

CATALOG: dict[str, dict[str, Any]] = {
    "google": {"provider": NativeProvider.GOOGLE, "name": "Google Workspace",
               "description": "Mail, calendar, drive, docs and more, read and act on your behalf.",
               "services": GOOGLE_SERVICES, "forget": ("gmail", "calendar"), "capability": Capability.DRIVE},
    "slack": {"provider": NativeProvider.SLACK, "name": "Slack",
              "description": "Read channels you invite Mavis to and message you there.",
              "services": SLACK_SERVICES, "forget": ("slack",), "capability": Capability.SLACK},
}


def native_provider() -> Any:
    from mavis.tools.integrations import get_provider

    return get_provider()


def _entry(cid: str, grant: Grant | None) -> dict:
    meta = CATALOG[cid]
    services: dict[str, frozenset[str]] = meta["services"]
    granted = [name for name, scopes in services.items() if grant and grant.scopes & scopes]
    if grant is None:
        status, account = "none", None
    else:
        status = "active" if grant.status == ACTIVE else "failed"
        account = (grant.account.get("email") if cid == "google"
                   else grant.account.get("team_name") or grant.account.get("team_id")) or None
    return {"id": cid, "name": meta["name"], "description": meta["description"], "status": status,
            "account": account, "scopes_granted": granted if status == "active" else [],
            "missing_scopes": [n for n in services if n not in granted] if status == "active" else [],
            "connected_at": grant.updated_at.isoformat() if grant and grant.updated_at else None}


async def connector_list(user_id: int, provider: Any) -> list[dict]:
    tokens = getattr(provider, "tokens", None)
    out = []
    for cid, meta in CATALOG.items():
        grant = await tokens.grant(user_id, meta["provider"]) if tokens is not None else None
        out.append(_entry(cid, grant))
    return out


@router.get("")
async def list_connectors(active: Active = Depends(common.authed), provider: Any = Depends(native_provider)
                          ) -> list[dict]:
    return await connector_list(active.user.id, provider)


def _meta(cid: str) -> dict[str, Any]:
    if cid not in CATALOG:
        raise common.not_found("That connector")
    return CATALOG[cid]


@router.post("/{cid}/connect")
async def connect(cid: str, active: Active = Depends(common.authed),
                  provider: Any = Depends(native_provider)) -> dict:
    meta = _meta(cid)
    oauth = getattr(provider, "oauth", None)
    if oauth is None or not oauth_mod.configured(meta["provider"]):
        raise DashError(503, "not_configured", f"{meta['name']} sign-in is not set up on this Mavis yet.")
    try:
        url = await oauth.authorize_url(active.user.id, meta["provider"], None, origin=WEB_ORIGIN,
                                        session_hash=active.session_id)
    except oauth_mod.OAuthError:
        raise DashError(503, "unavailable", f"{meta['name']} sign-in is not available right now.") from None
    return {"url": url}


class DisconnectBody(BaseModel):
    forget: bool = True


@router.post("/{cid}/disconnect", status_code=204)
async def disconnect(cid: str, body: DisconnectBody | None = None, active: Active = Depends(common.authed),
                     provider: Any = Depends(native_provider)) -> Response:
    from mavis.tools.integrations.wiring import get_connect_flow

    meta = _meta(cid)
    forget = True if body is None else body.forget
    tokens = getattr(provider, "tokens", None)
    if tokens is None or await tokens.grant(active.user.id, meta["provider"]) is None:
        raise DashError(404, "not_connected", f"{meta['name']} is not connected.")
    uid = active.user.id
    gone = await get_connect_flow().disconnect(uid, meta["capability"], forget=forget)
    if not gone:
        raise DashError(502, "disconnect_failed", f"Could not disconnect {meta['name']} just now. Try again.")
    if forget:
        for source in meta["forget"]:
            try:
                await get_vault().forget_source(uid, source)
            except VaultError:
                log.warning("dashboard.forget_source_failed", source=source)
    return Response(status_code=204)
