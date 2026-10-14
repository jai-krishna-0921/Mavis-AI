"""A fake OAuth authorization server (and the protected-resource metadata an MCP server publishes) for tests:
RFC 8414 metadata, RFC 7591 registration, and a token endpoint that checks PKCE and rotates refresh tokens.
It records every request so tests can assert on the wire."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass
class FakeAuthServer:
    base: str = ""  # set once the server is up: http://127.0.0.1:<port>
    issuer_override: str | None = None  # the metadata names a different issuer
    endpoint_host_override: str | None = None  # endpoints on another host (same-site violation)
    metadata_path: str = "/.well-known/oauth-authorization-server"  # or /.well-known/openid-configuration
    pkce_methods: list[str] | None = field(default_factory=lambda: ["S256"])
    registration: bool = True
    registration_status: int = 201
    token_status: int | None = None  # answer the token endpoint with this status
    rotate_refresh: bool = False
    expires_in: int | None = 3600
    refresh_delay_s: float = 0.0
    requests: list[dict] = field(default_factory=list)
    codes: dict[str, dict] = field(default_factory=dict)
    refresh_tokens: set[str] = field(default_factory=lambda: {"refresh-1"})
    issued: int = 0
    refresh_calls: int = 0

    def _record(self, request: Request, body: dict | None = None) -> None:
        self.requests.append({"method": request.method, "path": request.url.path, "body": body or {}})

    def issue_code(self, challenge: str, client_id: str = "client-1") -> str:
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = {"challenge": challenge, "client_id": client_id}
        return code

    @property
    def issuer(self) -> str:
        return self.issuer_override or self.base

    def _url(self, path: str) -> str:
        host = self.endpoint_host_override or self.base
        return f"{host}{path}"

    async def metadata(self, request: Request) -> Response:
        self._record(request)
        meta = {"issuer": self.issuer, "authorization_endpoint": self._url("/authorize"),
                "token_endpoint": self._url("/token")}
        if self.registration:
            meta["registration_endpoint"] = self._url("/register")
        if self.pkce_methods is not None:
            meta["code_challenge_methods_supported"] = self.pkce_methods
        return JSONResponse(meta)

    async def register(self, request: Request) -> Response:
        body = await request.json()
        self._record(request, body)
        if self.registration_status >= 400:
            return Response(status_code=self.registration_status, content=b"nope secret=abc123abc123")
        return JSONResponse({"client_id": "client-1", "client_id_issued_at": 1}, status_code=201)

    async def token(self, request: Request) -> Response:
        form = dict((await request.form()).items())
        self._record(request, form)
        if self.token_status:
            return Response(status_code=self.token_status,
                            content=b'{"error":"server_error","secret":"abc123"}')
        grant = form.get("grant_type")
        if grant == "authorization_code":
            entry = self.codes.pop(form.get("code", ""), None)
            if entry is None or s256(form.get("code_verifier", "")) != entry["challenge"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
        elif grant == "refresh_token":
            self.refresh_calls += 1
            if self.refresh_delay_s:
                await asyncio.sleep(self.refresh_delay_s)
            if form.get("refresh_token") not in self.refresh_tokens:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        self.issued += 1
        out: dict = {"access_token": f"access-{self.issued}", "token_type": "Bearer"}
        if self.expires_in is not None:
            out["expires_in"] = self.expires_in
        if grant == "authorization_code" or self.rotate_refresh:
            new = f"refresh-{self.issued + 1}"
            self.refresh_tokens.add(new)
            if self.rotate_refresh:
                self.refresh_tokens.discard(form.get("refresh_token", ""))
            out["refresh_token"] = new
        return JSONResponse(out)

    @property
    def app(self) -> Starlette:
        return Starlette(routes=[
            Route(self.metadata_path, self.metadata),
            Route("/register", self.register, methods=["POST"]),
            Route("/token", self.token, methods=["POST"]),
        ])
