# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous
# Research, MIT License.
"""The MCP server service: everything a user can do with their servers, and the one place tools and the
dashboard go through. Every method takes the user id and reaches rows only through it.

* create (catalog or custom URL): probe, and start OAuth when the server asks for it;
* connect (reconnect after NEEDS_AUTH), complete the OAuth callback, refresh the tool cache, delete;
* resolve a server and tool for a call (one cache refresh on a miss), check arguments, call;
* status transitions (D12): a 401 or 403 gets one token refresh for OAuth, else NEEDS_AUTH; a permanent
  failure while connecting or listing sets ERROR with a short code; a transient one changes nothing.

At-most-once: a tool call is never retried after it may have reached the server, except a 401 (the request
was refused, not run) after a token refresh. A read tool is retried once on a transient error."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import structlog

from mavis.connectors import netguard
from mavis.domain import timeutil
from mavis.mcp import catalog, sealing, toolcache
from mavis.mcp.client import Endpoint, McpClient
from mavis.mcp.failures import Failure, Kind, McpFailure
from mavis.mcp.limits import (
    CALL_DEADLINE_S,
    LISTING_REFRESH_S,
    MAX_CONCURRENT_SESSIONS,
    NAME_MAX_CHARS,
    SESSION_WAIT_S,
    TOKEN_MAX_CHARS,
    URL_MAX_CHARS,
)
from mavis.mcp.messages import model_text, user_text
from mavis.mcp.oauth import McpOAuth, new_verifier
from mavis.mcp.render import render_call_result
from mavis.mcp.schema import check_arguments, drop_null_optionals
from mavis.mcp.slugs import unique_slug
from mavis.mcp.text import clean_description
from mavis.mcp.tokens import TokenProvider
from mavis.store.models import McpServer
from mavis.store.repo import mcp_servers as repo

log = structlog.get_logger(__name__)

LimitReached = repo.LimitReached


class InvalidInput(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field, self.message = field, message


class Duplicate(Exception):
    """The user already connected this address."""


class NotFound(Exception):
    pass


class ProbeFailed(Exception):
    """The server could not be reached or would not answer as an MCP server. Carries a short code."""

    def __init__(self, code: str, kind: Kind) -> None:
        super().__init__(code)
        self.code, self.kind = code, kind

    @property
    def message(self) -> str:
        return user_text(self.code)


class Refusal(Exception):
    """A tool call that cannot be made (unknown server or tool, wrong kind, bad arguments, server needs
    attention). `text` is the sentence for the model."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass(frozen=True)
class Resolved:
    server: McpServer
    tool: dict  # the cache entry: name, description, input_schema, kind


@dataclass(frozen=True)
class CallResult:
    text: str  # for the model: the rendered result, or a sentence saying why it did not complete
    ok: bool  # the call completed (a tool-reported error still counts as completed)
    tool_error: bool = False  # the server said isError
    unknown_outcome: bool = False
    code: str = ""  # a short failure code when not ok


@dataclass(frozen=True)
class Created:
    server: dict
    authorize_url: str | None = None


@dataclass(frozen=True)
class Completed:
    ok: bool
    user_id: int | None = None
    server_id: int | None = None
    code: str = ""


# Failures that prove the server did not run the call (nothing was sent, or it answered with a refusal).
# Any other failure of an act may have happened after the request reached the server, so the outcome is
# unknown whatever the classification.
NOT_RUN_CODES = frozenset({"unreachable", "rate_limited", "not_found", "not_mcp", "rejected", "blocked_url",
                           "rpc_error"})


def _clean_name(value: str) -> str:
    name = clean_description(value, NAME_MAX_CHARS)
    if not name:
        raise InvalidInput("name", "Give the server a name.")
    return name


def _clean_url(value: str) -> str:
    url = (value or "").strip()
    if not url or len(url) > URL_MAX_CHARS:
        raise InvalidInput("url", "Enter the server's https address.")
    try:
        netguard.validate_url(url)
    except netguard.UnsafeUrl:
        raise InvalidInput("url", user_text("blocked_url")) from None
    return url


def _clean_token(value: str | None) -> str | None:
    token = (value or "").strip()
    if not token:
        return None
    if len(token) > TOKEN_MAX_CHARS or any(ch.isspace() or ord(ch) < 33 or ord(ch) == 127 for ch in token):
        raise InvalidInput("bearer_token", "That token does not look right. Paste only the token.")
    return token


def host_of(url: str) -> str:
    return urlsplit(url).netloc


def view(row: McpServer) -> dict:
    """A server as the dashboard and Telegram show it: no URL path or query, no secrets."""
    tools = [{"name": t.get("name", ""), "kind": t.get("kind", "act"),
              "description": t.get("description", "")} for t in (row.tools or [])]
    return {
        "id": row.id, "slug": row.slug, "name": row.display_name, "host": host_of(row.url),
        "catalog_id": row.catalog_id, "auth": row.auth_kind, "status": row.status,
        "error": row.last_error, "error_text": user_text(row.last_error) if row.last_error else None,
        "tool_count": len(tools), "tools": tools,
        "tools_fetched_at": row.tools_fetched_at.isoformat() if row.tools_fetched_at else None,
    }


class McpService:
    def __init__(self, *, client: McpClient | None = None, oauth: McpOAuth | None = None,
                 tokens: TokenProvider | None = None) -> None:
        self.client = client or McpClient()
        self.oauth = oauth or McpOAuth()
        self.tokens = tokens or TokenProvider(
            self.oauth, allowed_hosts_for=lambda row: catalog.allowed_hosts_for_url(row.url))
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)

    # --- reading ----------------------------------------------------------------------------------

    async def list_servers(self, user_id: int) -> list[dict]:
        return [view(row) for row in await repo.list_for(user_id)]

    async def list_servers_rows(self, user_id: int) -> list[McpServer]:
        return await repo.list_for(user_id)

    async def get_view(self, user_id: int, server_id: int) -> dict:
        row = await repo.get(user_id, server_id)
        if row is None:
            raise NotFound
        return view(row)

    async def has_active(self, user_id: int) -> bool:
        return await repo.has_active(user_id)

    # --- one session, bounded ---------------------------------------------------------------------

    async def _slot(self):
        try:
            await asyncio.wait_for(self._slots.acquire(), SESSION_WAIT_S)
        except TimeoutError:
            raise McpFailure(Failure(Kind.TRANSIENT, "rate_limited")) from None

    async def _attempt(self, row: McpServer, op, used: list[str] | None = None):
        """Run `op(endpoint)` with the row's token. A 401 or 403 on an OAuth server gets one forced token
        refresh and one more try (the request was refused, not run, so repeating it is safe). `used`, when
        given, collects every secret sent, so a caller can redact what the live (refreshed) token was."""
        await self._slot()
        try:
            token = await self.tokens.access_token(row.user_id, row.id)
            known = (*sealing.secrets_of(row), *((token,) if token else ()))
            if used is not None:
                used.extend(known)
            try:
                return await op(Endpoint(row.url, bearer=token, secrets=known))
            except McpFailure as exc:
                if exc.kind is not Kind.AUTH or row.auth_kind != "oauth":
                    raise
                token = await self.tokens.access_token(row.user_id, row.id, force=True)
                known = (*sealing.secrets_of(row), *((token,) if token else ()))
                if used is not None:
                    used.extend(known)
                return await op(Endpoint(row.url, bearer=token, secrets=known))
        finally:
            self._slots.release()

    async def _apply(self, row: McpServer, exc: McpFailure, *, listing: bool) -> None:
        """The status change a failure causes (D12)."""
        if exc.kind is Kind.AUTH:
            await repo.set_status(row.user_id, row.id, repo.NEEDS_AUTH, exc.code[:32])
        elif exc.kind is Kind.PERMANENT and listing:
            await repo.set_status(row.user_id, row.id, repo.ERROR, exc.code[:32])

    # --- the tool cache ---------------------------------------------------------------------------

    async def refresh_tools(self, user_id: int, server_id: int) -> McpServer:
        """List the server's tools again and keep them. Raises McpFailure (after setting the status)."""
        row = await repo.get(user_id, server_id)
        if row is None:
            raise NotFound
        try:
            raw = await self._attempt(row, self.client.list_tools)
        except McpFailure as exc:
            await self._apply(row, exc, listing=True)
            raise
        await repo.save_tools(user_id, server_id, toolcache.build_entries(raw), timeutil.now())
        await repo.set_status(user_id, server_id, repo.ACTIVE, None)
        fresh = await repo.get(user_id, server_id)
        assert fresh is not None
        return fresh

    async def refresh_view(self, user_id: int, server_id: int) -> dict:
        """The dashboard's Refresh: the failure becomes the server's status, not an error response."""
        try:
            return view(await self.refresh_tools(user_id, server_id))
        except McpFailure:
            return await self.get_view(user_id, server_id)

    async def fresh_servers(self, user_id: int, slug: str | None = None) -> list[McpServer]:
        """The user's servers (optionally one), with stale ACTIVE caches refreshed for a short while; a
        refresh that fails or is slow leaves the cache as it was."""
        rows = [r for r in await repo.list_for(user_id) if slug is None or r.slug == slug]
        stale = [r for r in rows if r.status == repo.ACTIVE and toolcache.is_stale(r.tools_fetched_at)]
        if stale:
            async def quiet(r: McpServer) -> None:
                try:
                    await self.refresh_tools(user_id, r.id)
                except (McpFailure, NotFound):
                    pass

            try:
                async with asyncio.timeout(LISTING_REFRESH_S):
                    await asyncio.gather(*(quiet(r) for r in stale))
            except TimeoutError:
                log.info("mcp.listing_refresh_timeout", stale=len(stale))
            rows = [r for r in await repo.list_for(user_id) if slug is None or r.slug == slug]
        return rows

    # --- creating ---------------------------------------------------------------------------------

    async def create(self, user_id: int, *, catalog_id: str | None = None, name: str | None = None,
                     url: str | None = None, bearer_token: str | None = None) -> Created:
        """Add a server from the catalog or by address, probe it, and start sign-in when it asks for it."""
        entry = catalog.get(catalog_id) if catalog_id else None
        if catalog_id and entry is None:
            raise InvalidInput("catalog_id", "That server is not in the list.")
        if entry is not None:
            display, address, auth_kind, bearer = entry.name, entry.url, entry.auth, None
        else:
            display, address = _clean_name(name or ""), _clean_url(url or "")
            bearer = _clean_token(bearer_token)
            auth_kind = "bearer" if bearer else "none"
        existing = await repo.list_for(user_id)
        if any(r.url == address for r in existing):
            raise Duplicate
        slug = unique_slug(display, [r.slug for r in existing])
        try:
            row = await repo.create(
                user_id, slug=slug, display_name=display, url=address, auth_kind=auth_kind,
                catalog_id=entry.id if entry else None, bearer=bearer,
                status=repo.NEEDS_AUTH if auth_kind == "oauth" else repo.ACTIVE)
        except repo.SlugTaken:  # a parallel add took the slug
            raise Duplicate from None
        try:
            if auth_kind == "oauth":
                return Created(view(row), await self._start_oauth(row))
            try:
                raw = await self._attempt(row, self.client.list_tools)
            except McpFailure as exc:
                if exc.kind is Kind.AUTH and auth_kind == "none":
                    return await self._oauth_after_challenge(row, exc.www_authenticate)
                raise
            await repo.save_tools(user_id, row.id, toolcache.build_entries(raw), timeutil.now())
            return Created(await self.get_view(user_id, row.id))
        except McpFailure as exc:
            await repo.delete_server(user_id, row.id)  # a server that never worked is not kept
            raise ProbeFailed(exc.code, exc.kind) from None
        except BaseException:
            await repo.delete_server(user_id, row.id)
            raise

    async def _oauth_after_challenge(self, row: McpServer, challenge: str | None) -> Created:
        """The probe got a 401: use OAuth if the server offers it, else it needs a token pasted."""
        try:
            authorize = await self._start_oauth(row, www_authenticate=challenge, switch=True)
        except McpFailure as exc:
            if exc.code in ("oauth_unsupported", "oauth_metadata", "oauth_discovery"):
                raise McpFailure(Failure(Kind.AUTH, "needs_token")) from None  # no OAuth: needs a token
            raise
        return Created(await self.get_view(row.user_id, row.id), authorize)

    async def _start_oauth(self, row: McpServer, *, www_authenticate: str | None = None,
                           switch: bool = False) -> str:
        """Discover, register (or reuse the stored client), store the facts and return the consent URL."""
        allowed = catalog.allowed_hosts_for_url(row.url)
        disc = await self.oauth.discover(row.url, www_authenticate=www_authenticate, allowed_hosts=allowed)
        stored = row.oauth or {}
        entry = catalog.get(row.catalog_id) if row.catalog_id else None
        same_issuer = stored.get("issuer") == disc.issuer
        if stored.get("issuer") and (not same_issuer or stored.get("token_endpoint") != disc.token_endpoint):
            # tokens belong to the issuer that gave them: never let them meet another issuer's endpoint
            await repo.clear_tokens(row.user_id, row.id)
        client_secret_sealed = stored.get(sealing.CLIENT_SECRET) if same_issuer else None
        if entry is not None and entry.client_id:
            client_id, registration = entry.client_id, "preregistered"
        elif stored.get("client_id") and stored.get("issuer") == disc.issuer:
            client_id, registration = stored["client_id"], stored.get("registration", "dcr")
        else:
            reg = await self.oauth.register(disc)
            client_id, registration = reg.client_id, "dcr"
            client_secret_sealed = sealing.seal(reg.client_secret, row.user_id, row.id, sealing.CLIENT_SECRET)
        facts = disc.facts(client_id, client_secret_sealed, registration=registration)
        await repo.save_oauth(row.user_id, row.id, facts)
        if switch:
            await repo.switch_to_oauth(row.user_id, row.id)
        verifier = new_verifier()
        state = await repo.create_state(row.user_id, row.id, verifier)
        return McpOAuth.authorize_url(facts, state, verifier)

    # --- connecting again, and the callback -------------------------------------------------------

    async def connect(self, user_id: int, server_id: int, *, bearer_token: str | None = None) -> Created:
        """Reconnect: a fresh consent URL for an OAuth server; for a token server, a new token."""
        row = await repo.get(user_id, server_id)
        if row is None:
            raise NotFound
        if row.auth_kind == "oauth":
            try:
                return Created(view(row), await self._start_oauth(row))
            except McpFailure as exc:
                raise ProbeFailed(exc.code, exc.kind) from None
        token = _clean_token(bearer_token)
        if row.auth_kind != "bearer" or token is None:
            raise InvalidInput("bearer_token", "Paste the server's token to reconnect it.")
        await repo.save_tokens(user_id, server_id, access=token, refresh=None, expires_at=None)
        try:
            await self.refresh_tools(user_id, server_id)
        except McpFailure as exc:
            raise ProbeFailed(exc.code, exc.kind) from None
        return Created(await self.get_view(user_id, server_id))

    async def complete_oauth(self, session_user_id: int | None, state: str, code: str, *,
                             iss: str | None = None) -> Completed:
        """The consent callback. The state is spent whoever presents it; it only counts for the signed-in
        user it was issued to. Only the ids stored with the state are trusted. `iss` is the callback's issuer
        parameter (RFC 9207)."""
        got = await repo.consume_state(state)
        if got is None:
            return Completed(False, code="bad_state")
        user_id, server_id, verifier = got
        if session_user_id is None or session_user_id != user_id:
            return Completed(False, user_id, server_id, "session_mismatch")
        row = await repo.get(user_id, server_id)
        if row is None or row.auth_kind != "oauth" or not row.oauth:
            return Completed(False, user_id, server_id, "not_found")
        try:
            got_tokens = await self.oauth.exchange(row.oauth, sealing.client_secret_of(row), code, verifier,
                                                  iss=iss)
        except McpFailure as exc:
            await repo.set_status(user_id, server_id, repo.NEEDS_AUTH, exc.code[:32])
            return Completed(False, user_id, server_id, exc.code)
        await repo.save_tokens(user_id, server_id, access=got_tokens.access_token,
                               refresh=got_tokens.refresh_token, expires_at=got_tokens.expires_at,
                               keep_refresh=False)
        await repo.set_status(user_id, server_id, repo.ACTIVE, None)
        try:
            await self.refresh_tools(user_id, server_id)
        except McpFailure as exc:
            return Completed(False, user_id, server_id, exc.code)
        return Completed(True, user_id, server_id)

    async def deny(self, state: str) -> int | None:
        """The user declined at the provider: spend the state; the server stays NEEDS_AUTH."""
        got = await repo.consume_state(state)
        return got[0] if got else None

    async def delete(self, user_id: int, server_id: int) -> bool:
        return await repo.delete_server(user_id, server_id)

    # --- calling ----------------------------------------------------------------------------------

    async def resolve(self, user_id: int, slug: str, tool_name: str) -> Resolved:
        """The server and cached tool for a call. An unknown server or tool triggers one cache refresh and
        then an error (D7). Raises Refusal."""
        row = await repo.by_slug(user_id, (slug or "").strip().lower())
        if row is None:
            names = ", ".join(r.slug for r in await repo.list_for(user_id)) or "none"
            raise Refusal(f"There is no connected server called '{clean_description(slug, 40)}'. "
                          f"Connected servers: {names}. Use mcp_tools to see them.")
        if row.status != repo.ACTIVE:
            raise Refusal(model_text(row.display_name, row.last_error or "auth",
                                     Kind.AUTH if row.status == repo.NEEDS_AUTH else Kind.PERMANENT))
        entry = toolcache.find(row.tools or [], tool_name)
        if entry is None:
            try:
                row = await self.refresh_tools(user_id, row.id)
            except (McpFailure, NotFound) as exc:
                code = exc.code if isinstance(exc, McpFailure) else "unknown"
                kind = exc.kind if isinstance(exc, McpFailure) else Kind.PERMANENT
                raise Refusal(model_text(row.display_name, code, kind)) from None
            entry = toolcache.find(row.tools or [], tool_name)
        if entry is None:
            raise Refusal(f"{row.display_name} has no tool called '{clean_description(tool_name, 64)}'. "
                          "Use mcp_tools to see its tools.")
        return Resolved(row, entry)

    @staticmethod
    def check(resolved: Resolved, arguments: dict) -> tuple[dict, str | None]:
        """(arguments with model-written nulls dropped, a problem sentence or None)."""
        schema = resolved.tool.get("input_schema") or {}
        cleaned = drop_null_optionals(schema, arguments or {})
        return cleaned, check_arguments(schema, cleaned)

    async def call(self, resolved: Resolved, arguments: dict, *, retry_transient: bool,
                   deadline_s: float = CALL_DEADLINE_S) -> CallResult:
        """Make the call. Never raises McpFailure: a failure becomes a CallResult the tool can report."""
        row, tool = resolved.server, resolved.tool["name"]

        async def op(ep: Endpoint):
            return await self.client.call_tool(ep, tool, arguments, deadline_s=deadline_s)

        tries = 2 if retry_transient else 1
        used: list[str] = list(sealing.secrets_of(row))
        for attempt in range(tries):
            try:
                result = await self._attempt(row, op, used)
            except McpFailure as exc:
                await self._apply(row, exc, listing=False)
                if exc.kind is Kind.TRANSIENT and attempt + 1 < tries:
                    await asyncio.sleep(0.5)
                    continue
                unknown = not retry_transient and exc.kind is not Kind.AUTH and (
                    exc.failure.unknown_outcome or exc.code not in NOT_RUN_CODES)
                text = model_text(row.display_name, exc.code, exc.kind, unknown_outcome=unknown, tool=tool)
                if exc.code == "rpc_error" and exc.detail:  # the server's own (sanitized, short) reason
                    text += f" The server said: {exc.detail}"
                return CallResult(text, ok=False, unknown_outcome=unknown, code=exc.code)
            rendered = render_call_result(result, secrets=tuple(dict.fromkeys(used)))
            return CallResult(rendered.text, ok=True, tool_error=rendered.is_error)
        raise AssertionError("unreachable")  # pragma: no cover


_SERVICE: McpService | None = None


def get_service() -> McpService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = McpService()
    return _SERVICE


def set_service(service: McpService | None) -> None:
    global _SERVICE
    _SERVICE = service


def now() -> datetime:  # pragma: no cover - re-export for callers that format times
    return timeutil.now()
