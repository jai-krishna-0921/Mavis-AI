# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""OAuth for remote MCP servers (spec D4): our own, stateless across requests, not the SDK's provider (its
redirect and callback handlers wait inside one coroutine, which cannot survive a browser redirect through
another request or process). Standards used:

* discovery: RFC 9728 protected-resource metadata (the `resource_metadata` pointer in a 401's
  `WWW-Authenticate`, else `/.well-known/oauth-protected-resource`), then RFC 8414 authorization-server
  metadata with the OpenID configuration as the fallback; a server that publishes no resource metadata is
  taken to be its own authorization server (the 2025-03-26 MCP revision);
* RFC 7591 dynamic client registration as a public client (`token_endpoint_auth_method: none`), or a client id
  the catalog pre-registers;
* authorization code with PKCE S256 and the `resource` parameter (RFC 8707);
* refresh, with the issuer recorded at connect time: a refresh against a different issuer is refused.

Every request goes through the net guard (https, public addresses, no redirects, capped bodies). The
authorization and token endpoints a server names must be https and `same_site` with the issuer (or a host the
catalog declares). Nothing here logs or raises with a token, a client secret, a response body or a URL query:
failures are `McpFailure`s with a fixed code."""

from __future__ import annotations

import base64
import hashlib
import math
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import structlog

from mavis.config import get_settings
from mavis.connectors import netguard
from mavis.domain import timeutil
from mavis.mcp.failures import Failure, Kind, McpFailure

log = structlog.get_logger(__name__)

CLIENT_NAME = "Mavis AI"
DISCOVERY_TTL_S = 600.0
DISCOVERY_CACHE_MAX = 256
MAX_EXPIRES_IN_S = 365 * 86400
_RESOURCE_METADATA = re.compile(r'resource_metadata="([^"]+)"')
_SCOPE = re.compile(r'(?<![\w-])scope="([^"]+)"')


def _fail(kind: Kind, code: str) -> McpFailure:
    return McpFailure(Failure(kind, code))


def new_verifier() -> str:
    """A PKCE code verifier (RFC 7636: 43 to 128 characters)."""
    return secrets.token_urlsafe(48)


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def redirect_uri() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/mcp/oauth/callback"


@dataclass(frozen=True)
class Discovery:
    """What the server told us about how to authorize, validated."""

    resource: str  # the MCP server URL (sent as the RFC 8707 `resource`)
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    scopes: tuple[str, ...] = ()
    iss_supported: bool = False  # RFC 9207: the callback carries `iss`

    def facts(self, client_id: str, client_secret_sealed: str | None, *, registration: str) -> dict[str, Any]:
        """The non-secret OAuth facts stored on the server row (`McpServer.oauth`)."""
        return {"issuer": self.issuer, "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint, "client_id": client_id,
                "client_secret": client_secret_sealed, "scopes": list(self.scopes),
                "resource": self.resource, "registration": registration,
                "iss_parameter_supported": self.iss_supported}


@dataclass(frozen=True)
class Registration:
    client_id: str
    client_secret: str | None = None


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scope: str | None = None


@dataclass
class McpOAuth:
    """One instance serves everyone; the discovery cache is the only state."""

    http_factory: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]] = field(
        default=lambda max_bytes: netguard.guarded_client(max_bytes=max_bytes))
    _cache: OrderedDict[tuple, tuple[float, Discovery]] = field(default_factory=OrderedDict)

    # --- one guarded request ----------------------------------------------------------------------

    async def _send(self, method: str, url: str, *, max_bytes: int, code: str, **kw: Any) -> httpx.Response:
        """One guarded call. A network or policy failure becomes a classified `McpFailure` raised `from None`,
        so no request (with its headers or body) is chained into the error."""
        try:
            async with self.http_factory(max_bytes) as http:
                return await http.request(method, url, **kw)
        except netguard.UnsafeUrl:
            raise _fail(Kind.PERMANENT, "blocked_url") from None
        except netguard.ResponseTooLarge:
            raise _fail(Kind.PERMANENT, "too_large") from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise _fail(Kind.TRANSIENT, "unreachable") from None

    @staticmethod
    def _json(r: httpx.Response) -> Any:
        """The response as JSON, or None when it is not (including nesting deep enough to overflow)."""
        try:
            return r.json()
        except (ValueError, RecursionError):
            return None

    async def _get_json(self, url: str, *, what: str) -> dict | None:
        """A JSON object at `url`, or None for an answer that is not one (404, HTML, a list)."""
        r = await self._send("GET", url, max_bytes=netguard.DISCOVERY_BYTES, code=what,
                             headers={"Accept": "application/json"})
        if r.status_code >= 500 or r.status_code == 429:
            raise _fail(Kind.TRANSIENT, what)
        if not 200 <= r.status_code < 300:
            return None
        body = self._json(r)
        return body if isinstance(body, dict) else None

    # --- discovery --------------------------------------------------------------------------------

    @staticmethod
    def _endpoint(value: object, issuer: str, allowed: tuple[str, ...]) -> str:
        """An authorization or token endpoint: a public https URL on the issuer's site or a declared host."""
        if not isinstance(value, str) or len(value) > 2048:
            raise _fail(Kind.PERMANENT, "oauth_metadata")
        try:
            parts = netguard.validate_url(value)
        except netguard.UnsafeUrl:
            raise _fail(Kind.PERMANENT, "oauth_metadata") from None
        if not netguard.same_site(parts.hostname or "", urlsplit(issuer).hostname or "", allowed):
            raise _fail(Kind.PERMANENT, "oauth_not_same_site")
        return value

    @staticmethod
    def _well_known(base: str, suffix: str) -> list[str]:
        """The RFC 8414 / 9728 locations: the suffix inserted before the path first, then at the root."""
        parts = urlsplit(base)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path.rstrip("/")
        urls = [f"{origin}/.well-known/{suffix}{path}"] if path else []
        urls.append(f"{origin}/.well-known/{suffix}")
        return urls

    async def discover(self, server_url: str, *, www_authenticate: str | None = None,
                       allowed_hosts: tuple[str, ...] = (), scope: str | None = None) -> Discovery:
        pointer_url = _RESOURCE_METADATA.search(www_authenticate or "")
        challenge_scope_text = _SCOPE.search(www_authenticate or "")
        key = (server_url, pointer_url.group(1) if pointer_url else None,
               challenge_scope_text.group(1) if challenge_scope_text else scope,
               tuple(sorted(allowed_hosts)))
        cached = self._cache.get(key)
        if cached:
            if cached[0] > time.monotonic():
                self._cache.move_to_end(key)
                return cached[1]
            del self._cache[key]
        try:
            server = netguard.validate_url(server_url)
        except netguard.UnsafeUrl:
            raise _fail(Kind.PERMANENT, "blocked_url") from None
        origin = f"{server.scheme}://{server.netloc}"

        # 1. protected-resource metadata (RFC 9728): the 401's pointer, else the well-known locations
        pointer = _RESOURCE_METADATA.search(www_authenticate or "")
        challenge_scope = _SCOPE.search(www_authenticate or "")
        candidates = ([pointer.group(1)] if pointer
                      else self._well_known(server_url, "oauth-protected-resource"))
        prm: dict | None = None
        for url in candidates:
            try:
                netguard.validate_url(url)
            except netguard.UnsafeUrl:
                raise _fail(Kind.PERMANENT, "oauth_metadata") from None
            prm = await self._get_json(url, what="oauth_discovery")
            if prm is not None:
                break
        issuer = origin  # no resource metadata: the server is its own authorization server
        scopes: tuple[str, ...] = ()
        if prm is not None:
            resource = prm.get("resource")
            res = urlsplit(resource) if isinstance(resource, str) else None
            if res is not None and f"{res.scheme}://{res.netloc}" != origin:
                raise _fail(Kind.PERMANENT, "oauth_metadata")  # metadata for a different server
            servers = prm.get("authorization_servers")
            if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
                raise _fail(Kind.PERMANENT, "oauth_metadata")
            issuer = servers[0].rstrip("/")
            supported = prm.get("scopes_supported")
            if isinstance(supported, list):
                scopes = tuple(str(s) for s in supported if isinstance(s, str))[:20]
        if challenge_scope:
            scopes = tuple(challenge_scope.group(1).split())[:20]
        elif scope:
            scopes = tuple(scope.split())[:20]
        try:
            netguard.validate_url(issuer)
        except netguard.UnsafeUrl:
            raise _fail(Kind.PERMANENT, "oauth_metadata") from None

        # 2. authorization-server metadata (RFC 8414), then OpenID configuration
        meta: dict | None = None
        for url in (*self._well_known(issuer, "oauth-authorization-server"),
                    f"{issuer}/.well-known/openid-configuration"):
            meta = await self._get_json(url, what="oauth_discovery")
            if meta is not None:
                break
        if meta is None:
            raise _fail(Kind.PERMANENT, "oauth_unsupported")
        if str(meta.get("issuer", "")).rstrip("/") != issuer:  # RFC 8414 section 3.3
            raise _fail(Kind.PERMANENT, "oauth_metadata")
        methods = meta.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise _fail(Kind.PERMANENT, "pkce_unsupported")
        found = Discovery(
            resource=server_url, issuer=issuer,
            authorization_endpoint=self._endpoint(meta.get("authorization_endpoint"), issuer, allowed_hosts),
            token_endpoint=self._endpoint(meta.get("token_endpoint"), issuer, allowed_hosts),
            registration_endpoint=(self._endpoint(meta["registration_endpoint"], issuer, allowed_hosts)
                                   if meta.get("registration_endpoint") else None),
            scopes=scopes,
            iss_supported=meta.get("authorization_response_iss_parameter_supported") is True)
        self._cache[key] = (time.monotonic() + DISCOVERY_TTL_S, found)  # only successes are cached
        while len(self._cache) > DISCOVERY_CACHE_MAX:
            self._cache.popitem(last=False)
        return found

    # --- client registration (RFC 7591) -----------------------------------------------------------

    async def register(self, disc: Discovery) -> Registration:
        if not disc.registration_endpoint:
            raise _fail(Kind.PERMANENT, "no_registration")
        body = {"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri()],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}
        if disc.scopes:
            body["scope"] = " ".join(disc.scopes)
        r = await self._send("POST", disc.registration_endpoint, max_bytes=netguard.TOKEN_BYTES,
                             code="oauth_registration", json=body)
        if r.status_code >= 500 or r.status_code == 429:
            raise _fail(Kind.TRANSIENT, "oauth_registration")
        data = self._json(r) if 200 <= r.status_code < 300 else None
        client_id = data.get("client_id") if isinstance(data, dict) else None
        if not isinstance(client_id, str) or not client_id or len(client_id) > 512:
            raise _fail(Kind.PERMANENT, "oauth_registration")
        secret = data.get("client_secret")
        return Registration(client_id, secret if isinstance(secret, str) and secret else None)

    # --- the consent redirect ---------------------------------------------------------------------

    @staticmethod
    def authorize_url(facts: dict, state: str, verifier: str) -> str:
        """The consent URL for a server whose OAuth facts (`Discovery.facts`) are stored."""
        query = {"response_type": "code", "client_id": facts["client_id"], "redirect_uri": redirect_uri(),
                 "state": state, "code_challenge": challenge_of(verifier), "code_challenge_method": "S256",
                 "resource": facts["resource"]}
        if facts.get("scopes"):
            query["scope"] = " ".join(facts["scopes"])
        endpoint = facts["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(query)}"

    # --- tokens -----------------------------------------------------------------------------------

    @staticmethod
    def _tokens(data: Any) -> TokenResponse:
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise _fail(Kind.PERMANENT, "oauth_token")
        if str(data.get("token_type", "bearer")).lower() != "bearer":
            raise _fail(Kind.PERMANENT, "oauth_token")
        refresh = data.get("refresh_token")
        ttl = 0
        raw = data.get("expires_in")
        if raw is not None and not isinstance(raw, bool):
            try:
                seconds = float(raw)
            except (TypeError, ValueError, OverflowError):
                seconds = 0.0
            if math.isfinite(seconds):
                ttl = int(min(seconds, MAX_EXPIRES_IN_S))
        scope = data.get("scope")
        return TokenResponse(
            access_token=token, refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=timeutil.now() + timedelta(seconds=ttl) if ttl > 0 else None,
            scope=scope if isinstance(scope, str) else None)

    async def _token_call(self, facts: dict, client_secret: str | None,
                          form: dict[str, str]) -> TokenResponse:
        form = {**form, "client_id": facts["client_id"], "resource": facts["resource"]}
        if client_secret:
            form["client_secret"] = client_secret
        r = await self._send("POST", facts["token_endpoint"], max_bytes=netguard.TOKEN_BYTES,
                             code="oauth_token", data=form, headers={"Accept": "application/json"})
        if r.status_code >= 500 or r.status_code == 429:
            raise _fail(Kind.TRANSIENT, "oauth_token")
        if r.status_code in (400, 401, 403):
            raise _fail(Kind.AUTH, "oauth_token")  # invalid_grant, invalid_client: the user must reconnect
        if not 200 <= r.status_code < 300:
            raise _fail(Kind.PERMANENT, "oauth_token")
        data = self._json(r)
        if data is None:
            raise _fail(Kind.PERMANENT, "oauth_token")
        return self._tokens(data)

    @staticmethod
    def check_iss(facts: dict, iss: str | None) -> None:
        """RFC 9207 mix-up defence: an `iss` on the callback must equal the issuer recorded at connect time,
        and is required when the authorization server advertises that it sends one."""
        if iss is None:
            if facts.get("iss_parameter_supported") is True:
                raise _fail(Kind.AUTH, "issuer_mismatch")
            return
        if not isinstance(iss, str) or iss.rstrip("/") != str(facts.get("issuer", "")).rstrip("/"):
            raise _fail(Kind.AUTH, "issuer_mismatch")

    async def exchange(self, facts: dict, client_secret: str | None, code: str, verifier: str, *,
                       iss: str | None = None) -> TokenResponse:
        """Trade the authorization code (with the PKCE verifier) for tokens. `iss` is the callback's issuer
        parameter (RFC 9207); a mismatch, or a missing one the server promised, is refused up front."""
        self.check_iss(facts, iss)
        return await self._token_call(facts, client_secret, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "code_verifier": verifier})

    async def refresh(self, facts: dict, client_secret: str | None, refresh_token: str, *,
                      allowed_hosts: tuple[str, ...] = (),
                      www_authenticate: str | None = None) -> TokenResponse:
        """Refresh an access token. The issuer recorded at connect time must still be the one the server
        names, and the token endpoint must still sit on the issuer's site: a server cannot move our
        refresh token to another issuer."""
        disc = await self.discover(facts["resource"], www_authenticate=www_authenticate,
                                   allowed_hosts=allowed_hosts)
        if disc.issuer != facts["issuer"]:
            raise _fail(Kind.AUTH, "issuer_changed")
        try:
            self._endpoint(facts["token_endpoint"], facts["issuer"], allowed_hosts)
        except McpFailure:
            raise _fail(Kind.AUTH, "issuer_changed") from None
        return await self._token_call(facts, client_secret, {
            "grant_type": "refresh_token", "refresh_token": refresh_token})
