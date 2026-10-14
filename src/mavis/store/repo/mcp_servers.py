# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The MCP servers a user connected, and the consent redirects in flight.

Every function takes the user id and filters by it: a server id alone never reaches a row, so one user can
never read, change or delete another user's server. Secrets are sealed here (mavis.mcp.sealing) and never
returned in plain form; callers that need a token open it with `sealing.tokens_of(row)`."""

from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from mavis.domain import timeutil
from mavis.mcp import sealing
from mavis.mcp.limits import MAX_SERVERS_PER_USER, STATE_TTL
from mavis.store import db as dbm
from mavis.store.models import McpOAuthState, McpServer

ACTIVE, NEEDS_AUTH, ERROR = "ACTIVE", "NEEDS_AUTH", "ERROR"


class LimitReached(Exception):
    """The user already has MAX_SERVERS_PER_USER servers."""


class SlugTaken(Exception):
    """The user already has a server with this slug."""


async def list_for(user_id: int) -> list[McpServer]:
    async with dbm.Session() as s:
        rows = await s.scalars(select(McpServer).where(McpServer.user_id == user_id).order_by(McpServer.id))
        return list(rows)


async def count(user_id: int) -> int:
    async with dbm.Session() as s:
        return int(await s.scalar(select(func.count()).select_from(McpServer).where(
            McpServer.user_id == user_id)) or 0)


async def has_active(user_id: int) -> bool:
    async with dbm.Session() as s:
        found = await s.scalar(select(McpServer.id).where(
            McpServer.user_id == user_id, McpServer.status == ACTIVE).limit(1))
        return found is not None


async def get(user_id: int, server_id: int) -> McpServer | None:
    async with dbm.Session() as s:
        return await s.scalar(select(McpServer).where(
            McpServer.id == server_id, McpServer.user_id == user_id))


async def by_slug(user_id: int, slug: str) -> McpServer | None:
    async with dbm.Session() as s:
        return await s.scalar(select(McpServer).where(McpServer.slug == slug, McpServer.user_id == user_id))


async def create(user_id: int, *, slug: str, display_name: str, url: str, auth_kind: str,
                 catalog_id: str | None = None, bearer: str | None = None, status: str = ACTIVE,
                 oauth: dict | None = None) -> McpServer:
    """Insert a server. The per-user limit is checked in the same transaction; a clashing slug raises
    SlugTaken. A bearer token is sealed once the row has an id (the id is part of what it is bound to)."""
    now = timeutil.now()
    async with dbm.Session() as s:
        total = await s.scalar(select(func.count()).select_from(McpServer).where(
            McpServer.user_id == user_id))
        if (total or 0) >= MAX_SERVERS_PER_USER:
            raise LimitReached
        row = McpServer(user_id=user_id, slug=slug, display_name=display_name, url=url, catalog_id=catalog_id,
                        auth_kind=auth_kind, status=status, oauth=oauth or {}, tools=[], created_at=now,
                        updated_at=now)
        s.add(row)
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            raise SlugTaken from None
        if bearer:
            row.access_token = sealing.seal(bearer, user_id, row.id, sealing.ACCESS_TOKEN)
        await s.commit()
        return row


async def set_status(user_id: int, server_id: int, status: str, last_error: str | None = None) -> None:
    async with dbm.Session() as s:
        await s.execute(update(McpServer).where(McpServer.id == server_id, McpServer.user_id == user_id)
                        .values(status=status, last_error=last_error, updated_at=timeutil.now()))
        await s.commit()


async def switch_to_oauth(user_id: int, server_id: int) -> None:
    """A server added as open turned out to want sign-in: it is an OAuth server awaiting consent."""
    async with dbm.Session() as s:
        await s.execute(update(McpServer).where(McpServer.id == server_id, McpServer.user_id == user_id)
                        .values(auth_kind="oauth", status=NEEDS_AUTH, last_error=None,
                                updated_at=timeutil.now()))
        await s.commit()


async def save_tools(user_id: int, server_id: int, tools: list[dict], fetched_at: datetime) -> None:
    async with dbm.Session() as s:
        await s.execute(update(McpServer).where(McpServer.id == server_id, McpServer.user_id == user_id)
                        .values(tools=tools, tools_fetched_at=fetched_at, updated_at=timeutil.now()))
        await s.commit()


async def save_oauth(user_id: int, server_id: int, oauth: dict) -> None:
    """Replace the row's OAuth facts (issuer, endpoints, client id, sealed client secret, scopes)."""
    async with dbm.Session() as s:
        await s.execute(update(McpServer).where(McpServer.id == server_id, McpServer.user_id == user_id)
                        .values(oauth=oauth, updated_at=timeutil.now()))
        await s.commit()


async def save_tokens(user_id: int, server_id: int, *, access: str, refresh: str | None,
                      expires_at: datetime | None, keep_refresh: bool = False) -> None:
    """Store a fresh token pair. `keep_refresh`: leave the stored refresh token alone when the server sent
    none (most servers do not rotate it)."""
    values = {"access_token": sealing.seal(access, user_id, server_id, sealing.ACCESS_TOKEN),
              "expires_at": expires_at, "updated_at": timeutil.now()}
    if refresh is not None or not keep_refresh:
        values["refresh_token"] = sealing.seal(refresh, user_id, server_id, sealing.REFRESH_TOKEN)
    async with dbm.Session() as s:
        await s.execute(update(McpServer).where(McpServer.id == server_id, McpServer.user_id == user_id)
                        .values(**values))
        await s.commit()


async def delete_server(user_id: int, server_id: int) -> bool:
    """Delete the row, its tokens with it, and any consent in flight. True when a row was deleted."""
    async with dbm.Session() as s:
        row = await s.scalar(select(McpServer.id).where(McpServer.id == server_id,
                                                        McpServer.user_id == user_id))
        if row is None:
            return False
        await s.execute(delete(McpOAuthState).where(McpOAuthState.server_id == server_id,
                                                    McpOAuthState.user_id == user_id))
        await s.execute(delete(McpServer).where(McpServer.id == server_id, McpServer.user_id == user_id))
        await s.commit()
        return True


# --- consent redirects in flight ------------------------------------------------------------------

async def create_state(user_id: int, server_id: int, verifier: str) -> str:
    """A fresh single-use `state` for one consent redirect; the PKCE verifier is kept sealed. Expired rows
    are purged on the way."""
    state = secrets.token_urlsafe(32)
    now = timeutil.now()
    async with dbm.Session() as s:
        await s.execute(delete(McpOAuthState).where(McpOAuthState.expires_at < now))
        s.add(McpOAuthState(state=state, user_id=user_id, server_id=server_id,
                            code_verifier=sealing.seal(verifier, user_id, server_id, sealing.CODE_VERIFIER),
                            expires_at=now + STATE_TTL, created_at=now))
        await s.commit()
    return state


async def consume_state(state: str) -> tuple[int, int, str] | None:
    """(user_id, server_id, code_verifier) for a valid state, deleting it; None when unknown, expired or
    already used. The state is spent whoever presents it."""
    if not state or len(state) > 64:
        return None
    async with dbm.Session() as s:
        row = await s.scalar(select(McpOAuthState).where(McpOAuthState.state == state))
        if row is None:
            return None
        res = await s.execute(delete(McpOAuthState).where(McpOAuthState.state == state))
        await s.commit()
        if not res.rowcount or timeutil.ensure_utc(row.expires_at) <= timeutil.now():
            return None
        verifier = sealing.unseal(row.code_verifier, row.user_id, row.server_id, sealing.CODE_VERIFIER)
        return row.user_id, row.server_id, verifier or ""
