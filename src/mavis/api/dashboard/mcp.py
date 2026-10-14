# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""MCP tool servers on the dashboard (spec D13): the catalog, the user's servers, connecting, the OAuth
callback, refreshing and removing. Every route needs the dashboard session, and the user comes from it, never
from the request. The whole router answers 404 unless MCP_ENABLED."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from mavis.api import ratelimit
from mavis.api.dashboard import common
from mavis.api.dashboard.common import DashError
from mavis.config import get_settings
from mavis.mcp import catalog
from mavis.mcp import service as svc
from mavis.mcp.failures import Kind
from mavis.web import sessions
from mavis.web.sessions import Active

log = structlog.get_logger(__name__)

WRITES_PER_MIN = 12  # adds, connects, refreshes per session: each can open a connection to another host
CALLBACKS_PER_MIN = 30


async def enabled() -> None:
    if not get_settings().mcp_enabled:
        raise DashError(404, "not_found", "Not found.")


router = APIRouter(prefix="/mcp", dependencies=[Depends(enabled)])


async def _throttle(active: Active) -> None:
    if not await ratelimit.hit("mcp-write", active.session_id, WRITES_PER_MIN):
        raise DashError(429, "rate_limited", "Too many attempts. Wait a minute and try again.")


def _created(made: svc.Created) -> dict:
    return {"server": made.server, **({"authorize_url": made.authorize_url} if made.authorize_url else {})}


def _probe_error(exc: svc.ProbeFailed) -> DashError:
    status = 502 if exc.kind is Kind.TRANSIENT else 422
    return DashError(status, "probe_failed", exc.message)


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog_id: str | None = Field(default=None, max_length=40)
    name: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=4096)
    bearer_token: str | None = Field(default=None, max_length=8192)


class ConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bearer_token: str | None = Field(default=None, max_length=8192)


@router.get("/catalog")
async def get_catalog(active: Active = Depends(common.authed)) -> list[dict]:
    return catalog.as_dicts()


@router.get("/servers")
async def list_servers(active: Active = Depends(common.authed)) -> list[dict]:
    return await svc.get_service().list_servers(active.user.id)


@router.post("/servers", status_code=201)
async def create_server(body: CreateBody, active: Active = Depends(common.authed)) -> dict:
    await _throttle(active)
    try:
        made = await svc.get_service().create(
            active.user.id, catalog_id=body.catalog_id, name=body.name, url=body.url,
            bearer_token=body.bearer_token)
    except svc.InvalidInput as exc:
        raise DashError(422, f"invalid_{exc.field}", exc.message) from None
    except svc.LimitReached:
        raise DashError(409, "limit_reached",
                        "You can connect up to 10 servers. Remove one to add another.") from None
    except svc.Duplicate:
        raise DashError(409, "duplicate", "That server is already connected.") from None
    except svc.ProbeFailed as exc:
        raise _probe_error(exc) from None
    return _created(made)


@router.post("/servers/{server_id}/connect")
async def connect_server(server_id: int, body: ConnectBody | None = None,
                         active: Active = Depends(common.authed)) -> dict:
    await _throttle(active)
    try:
        made = await svc.get_service().connect(active.user.id, server_id,
                                               bearer_token=body.bearer_token if body else None)
    except svc.NotFound:
        raise common.not_found("That server") from None
    except svc.InvalidInput as exc:
        raise DashError(422, f"invalid_{exc.field}", exc.message) from None
    except svc.ProbeFailed as exc:
        raise _probe_error(exc) from None
    return _created(made)


@router.post("/servers/{server_id}/refresh")
async def refresh_server(server_id: int, active: Active = Depends(common.authed)) -> dict:
    await _throttle(active)
    try:
        return await svc.get_service().refresh_view(active.user.id, server_id)
    except svc.NotFound:
        raise common.not_found("That server") from None


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: int, active: Active = Depends(common.authed)) -> Response:
    if not await svc.get_service().delete(active.user.id, server_id):
        raise common.not_found("That server")
    return Response(status_code=204)


def _back(result: str) -> RedirectResponse:
    """The browser returns to the Workspace page, which reads ?mcp= to say how it went."""
    return RedirectResponse(f"/workspace?mcp={result}", status_code=303,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/oauth/callback", dependencies=[Depends(common.limit_ip("mcp-cb", CALLBACKS_PER_MIN))])
async def oauth_callback(request: Request, code: str = "", state: str = "",
                         error: str = "", iss: str | None = Query(default=None, max_length=2048),
                         ) -> RedirectResponse:
    """The consent redirect. A navigation, so there is no CSRF header; the state is single use and counts
    only for the signed-in user it was issued to (RFC 9207 `iss` is passed on). Neither the provider's `error`
    text nor any id from the result is ever echoed."""
    service = svc.get_service()
    if error or not code:
        if state:
            await service.deny(state)
        return _back("error")
    active = await sessions.lookup(request.cookies.get(sessions.COOKIE))
    done = await service.complete_oauth(active.user.id if active is not None else None, state, code,
                                       iss=iss)
    if not done.ok:
        log.info("mcp.oauth_callback_failed", code=done.code)
    return _back("connected" if done.ok else "error")
