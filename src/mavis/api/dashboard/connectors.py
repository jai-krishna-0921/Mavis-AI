# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Connectors: every catalogued native provider, listed from the provider table. Reuses the router's grants
and ConnectFlow; the browser is sent to the same consent screen Telegram's /connect uses."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from mavis.api.dashboard import common
from mavis.api.dashboard.common import DashError
from mavis.memory.vault import VaultError, get_vault
from mavis.tools.integrations.actions import consent_notice
from mavis.tools.integrations.native import oauth as oauth_mod
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from mavis.web.sessions import Active

router = APIRouter(prefix="/connectors")
log = structlog.get_logger(__name__)

WEB_ORIGIN = "web"

def native_provider() -> Any:
    from mavis.tools.integrations import get_provider

    return get_provider()


def _listed() -> list[providers.ProviderSpec]:
    """Providers the page shows, in registration order. A provider whose id is not a NativeProvider member
    cannot hold a grant, so it is skipped."""
    out = []
    for spec in providers.user_providers():
        cat = spec.catalog
        if cat is None or not (cat.always_listed or providers.configured(spec)):
            continue
        try:
            NativeProvider(spec.id)
        except ValueError:
            continue
        out.append(spec)
    return out


def _entry(spec: providers.ProviderSpec, grant: Grant | None) -> dict:
    cat = spec.catalog
    services = cat.services
    granted = [name for name, scopes in services.items() if grant and grant.scopes & scopes]
    if grant is None:
        status, account = "none", None
    else:
        status = "active" if grant.status == ACTIVE else "failed"
        account = spec.account_label(grant.account) or None
    return {"id": spec.id, "name": cat.name or spec.display_name, "description": cat.description,
            "icon": cat.icon, "services": list(services) if cat.chips else [], "status": status,
            "account": account, "scopes_granted": granted if status == "active" else [],
            "missing_scopes": [n for n in services if n not in granted] if status == "active" else [],
            "connected_at": grant.updated_at.isoformat() if grant and grant.updated_at else None,
            "notice": consent_notice(spec.anchor) or None}


async def connector_list(user_id: int, provider: Any) -> list[dict]:
    tokens = getattr(provider, "tokens", None)
    out = []
    for spec in _listed():
        grant = await tokens.grant(user_id, NativeProvider(spec.id)) if tokens is not None else None
        out.append(_entry(spec, grant))
    return out


@router.get("")
async def list_connectors(active: Active = Depends(common.authed), provider: Any = Depends(native_provider)
                          ) -> list[dict]:
    return await connector_list(active.user.id, provider)


def _spec(cid: str) -> providers.ProviderSpec:
    spec = providers.spec_of(cid)
    if spec is None or spec.internal or spec.catalog is None:
        raise common.not_found("That connector")
    try:
        NativeProvider(spec.id)
    except ValueError:
        raise common.not_found("That connector") from None
    return spec


def _title(spec: providers.ProviderSpec) -> str:
    return spec.catalog.name or spec.display_name


@router.post("/{cid}/connect")
async def connect(cid: str, active: Active = Depends(common.authed),
                  provider: Any = Depends(native_provider)) -> dict:
    spec = _spec(cid)
    oauth = getattr(provider, "oauth", None)
    if oauth is None or not providers.configured(spec):
        raise DashError(503, "not_configured", f"{_title(spec)} sign-in is not set up on this Mavis yet.")
    try:
        url = await oauth.authorize_url(active.user.id, NativeProvider(spec.id), None, origin=WEB_ORIGIN,
                                        session_hash=active.session_id)
    except oauth_mod.OAuthError:
        raise DashError(503, "unavailable", f"{_title(spec)} sign-in is not available right now.") from None
    return {"url": url, "notice": consent_notice(spec.anchor) or None}


class DisconnectBody(BaseModel):
    forget: bool = True


@router.post("/{cid}/disconnect", status_code=204)
async def disconnect(cid: str, body: DisconnectBody | None = None, active: Active = Depends(common.authed),
                     provider: Any = Depends(native_provider)) -> Response:
    from mavis.tools.integrations.wiring import get_connect_flow

    spec = _spec(cid)
    forget = True if body is None else body.forget
    tokens = getattr(provider, "tokens", None)
    if tokens is None or await tokens.grant(active.user.id, NativeProvider(spec.id)) is None:
        raise DashError(404, "not_connected", f"{_title(spec)} is not connected.")
    uid = active.user.id
    gone = await get_connect_flow().disconnect(uid, spec.anchor, forget=forget)
    if not gone:
        raise DashError(502, "disconnect_failed", f"Could not disconnect {_title(spec)} just now. Try again.")
    if forget:
        for source in tuple(spec.memory_sources):
            try:
                await get_vault().forget_source(uid, source)
            except VaultError:
                log.warning("dashboard.forget_source_failed", source=source)
    return Response(status_code=204)
