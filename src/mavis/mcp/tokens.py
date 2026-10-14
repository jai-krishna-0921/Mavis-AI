# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous
# Research, MIT License.
"""Access tokens for a server row: none, a pasted bearer token, or an OAuth token kept fresh.

The refresh fence (the idea of Hermes's refresh serialization): concurrent refreshes for one server are
serialized by an asyncio lock inside a process and a row lock (`SELECT ... FOR UPDATE` on Postgres) across
processes, so two callers never both spend a rotating refresh token. A caller that waited for a refresh to
finish uses the new token instead of refreshing again. A token within 60 seconds of expiry is refreshed
first; a refresh the server refuses sets the server NEEDS_AUTH (the user reconnects)."""

from __future__ import annotations

import asyncio
import weakref

import structlog
from sqlalchemy import select

from mavis.domain import timeutil
from mavis.mcp import sealing
from mavis.mcp.failures import Failure, Kind, McpFailure
from mavis.mcp.limits import REFRESH_AHEAD
from mavis.mcp.oauth import McpOAuth
from mavis.store import db as dbm
from mavis.store.models import McpServer
from mavis.store.repo import mcp_servers as repo

log = structlog.get_logger(__name__)


def _needs_auth(code: str = "auth") -> McpFailure:
    return McpFailure(Failure(Kind.AUTH, code))


class TokenProvider:
    def __init__(self, oauth: McpOAuth, *, allowed_hosts_for=lambda row: ()) -> None:
        self.oauth = oauth
        self._allowed_hosts_for = allowed_hosts_for  # the catalog's declared hosts for a row
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self._generation: dict[int, int] = {}
        self._tasks: set[asyncio.Future] = set()

    def _lock(self, server_id: int) -> asyncio.Lock:
        lock = self._locks.get(server_id)
        if lock is None:
            lock = self._locks[server_id] = asyncio.Lock()
        return lock

    @staticmethod
    def _fresh(expires_at) -> bool:
        return expires_at is None or timeutil.ensure_utc(expires_at) - REFRESH_AHEAD > timeutil.now()

    async def access_token(self, user_id: int, server_id: int, *, force: bool = False,
                           www_authenticate: str | None = None) -> str | None:
        """The token to send, or None for a server that needs none. `force` refreshes now (after a 401;
        `www_authenticate` is that 401's challenge, whose resource_metadata pointer starts discovery).
        Raises McpFailure(AUTH) when the user must reconnect.

        The refresh and the save of the rotated tokens run as their own task that the caller only waits on
        through `asyncio.shield`: a caller that times out or is cancelled stops waiting, never the refresh,
        so a provider that already rotated the refresh token cannot lose the new one."""
        row = await repo.get(user_id, server_id)
        if row is None:
            raise McpFailure(Failure(Kind.PERMANENT, "not_found"))
        if row.auth_kind == "none":
            return None
        if row.auth_kind == "bearer":
            return sealing.tokens_of(row).access
        task = asyncio.ensure_future(
            self._fenced(user_id, server_id, force, self._generation.get(server_id, 0), www_authenticate))
        self._tasks.add(task)  # a strong reference until it finishes
        task.add_done_callback(self._finished)
        return await asyncio.shield(task)

    def _finished(self, task: asyncio.Future) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved: an abandoned waiter must not leave "never retrieved" noise

    async def _fenced(self, user_id: int, server_id: int, force: bool, seen: int,
                      www_authenticate: str | None) -> str | None:
        async with self._lock(server_id):
            if force and self._generation.get(server_id, 0) != seen:
                force = False  # a refresh finished while this caller waited: its token is the fresh one
            async with dbm.Session() as s:
                locked = await s.scalar(select(McpServer).where(
                    McpServer.id == server_id, McpServer.user_id == user_id).with_for_update())
                if locked is None:
                    raise McpFailure(Failure(Kind.PERMANENT, "not_found"))
                tokens = sealing.tokens_of(locked)
                if not tokens.access:
                    raise _needs_auth()
                if not force and self._fresh(locked.expires_at):
                    return tokens.access
                if not tokens.refresh:
                    await self._mark(s, locked, "auth")
                    raise _needs_auth()
                try:
                    got = await self.oauth.refresh(
                        locked.oauth, sealing.client_secret_of(locked), tokens.refresh,
                        allowed_hosts=tuple(self._allowed_hosts_for(locked)),
                        www_authenticate=www_authenticate)
                except McpFailure as exc:
                    if exc.kind is Kind.AUTH:
                        await self._mark(s, locked, exc.code)
                    raise
                locked.access_token = sealing.seal(got.access_token, user_id, server_id, sealing.ACCESS_TOKEN)
                if got.refresh_token:  # servers that rotate send a new one; otherwise keep the old
                    locked.refresh_token = sealing.seal(
                        got.refresh_token, user_id, server_id, sealing.REFRESH_TOKEN)
                locked.expires_at, locked.updated_at = got.expires_at, timeutil.now()
                locked.status, locked.last_error = repo.ACTIVE, None
                await s.commit()
                self._generation[server_id] = self._generation.get(server_id, 0) + 1
                return got.access_token

    @staticmethod
    async def _mark(session, row: McpServer, code: str) -> None:
        row.status, row.last_error, row.updated_at = repo.NEEDS_AUTH, code[:32], timeutil.now()
        await session.commit()
