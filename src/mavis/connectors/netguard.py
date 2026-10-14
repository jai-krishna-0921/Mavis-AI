# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The one outbound-URL guard for every connector and MCP HTTP call (discovery, registration, token, the MCP
endpoint itself, vendor APIs).

* https only, no credentials in the URL, no IP literals;
* the host is resolved and EVERY address must be public (no private, loopback, link-local, metadata,
  multicast, reserved or unspecified address, IPv4-mapped IPv6 included);
* DNS rebinding: the connection is made to the address that was just validated (`GuardedBackend` resolves,
  validates and connects in one step, so a second lookup can never return a different answer);
* redirects are never followed: a 3xx is an error;
* every response is capped while it streams: the read is aborted as soon as the cap is passed, before the
  body is buffered, and `Content-Length` over the cap is refused up front;
* bounded timeouts on connect, read, write and pool.

Two clients share one implementation: `guarded_client()` (httpx, for our own OAuth and discovery calls) and
`guarded_mcp_client()` (httpx2, the HTTP library the `mcp` SDK 2.x speaks, handed to its transports).

Dev and tests only: with MCP_ALLOW_LOCALHOST (never honoured when ENV=prod) the hosts `localhost`,
`127.0.0.1` and `::1` are accepted over http or https, so a fake server on this machine can be reached.

Endpoints a remote server names in its metadata must also be `same_site` as the issuer (or declared in the
catalog): see `mavis/mcp/oauth.py`."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpcore
import httpcore2
import httpx
import httpx2
from httpcore._backends.auto import AutoBackend
from httpcore2._backends.auto import AutoBackend as AutoBackend2

from mavis.config import get_settings
from mavis.domain.errors import IntegrationError

MAX_BYTES = 2_000_000
DISCOVERY_BYTES = 256_000
TOKEN_BYTES = 64_000
MCP_BYTES = 4_000_000  # one MCP response (D9)
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
MCP_TIMEOUT = httpx2.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class UnsafeUrl(IntegrationError):
    """The URL (or what its host resolves to) is not one we will call."""


class ResponseTooLarge(httpx.TransportError):
    """A response passed its size cap; the read was aborted."""


class SdkResponseTooLarge(httpx2.ReadError):
    """Same, seen by the MCP SDK's HTTP library (a ReadError, so the SDK's own error path handles it)."""


TOO_LARGE = (ResponseTooLarge, SdkResponseTooLarge)


async def _system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


_resolver = _system_resolver  # tests replace this; production resolves through the system resolver


def local_ok(host: str) -> bool:
    """A dev/test localhost host that the explicit setting lets through (never in prod)."""
    return host.lower().strip("[]") in LOCAL_HOSTS and get_settings().mcp_localhost_ok


def blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv6Address) and addr.sixtofour is not None:
        if blocked_ip(str(addr.sixtofour)):
            return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
            or addr.is_reserved or addr.is_unspecified or not addr.is_global)


def validate_url(url: str) -> SplitResult:
    """Syntax checks only (no network): https, a DNS name, no credentials."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        raise UnsafeUrl("not a valid url") from None
    if (host and local_ok(host) and parts.scheme in ("http", "https")
            and not (parts.username or parts.password)):
        return parts
    if parts.scheme != "https" or not host:
        raise UnsafeUrl("the url must be https")
    if parts.username or parts.password:
        raise UnsafeUrl("the url must not carry credentials")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeUrl("the url must use a host name, not an address")
    if host.lower() in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal")):
        raise UnsafeUrl("the url points at an internal host")
    return parts


async def resolve_public(host: str, port: int = 443) -> list[str]:
    try:
        ips = await _resolver(host, port)
    except OSError:
        raise UnsafeUrl("the host could not be resolved") from None
    if local_ok(host):
        if not ips:
            raise UnsafeUrl("the host could not be resolved")
        return ips
    if not ips or any(blocked_ip(ip) for ip in ips):
        raise UnsafeUrl("the host resolves to a non-public address")
    return ips


async def check_url(url: str) -> SplitResult:
    parts = validate_url(url)
    await resolve_public(parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80))
    return parts


def same_site(host: str, base: str, allowed: tuple[str, ...] = ()) -> bool:
    """`host` may be named by a server whose own host is `base`: the same host, a subdomain of it, a parent
    of it, a sibling under a common parent of at least three labels (auth.shop.example.com for
    mcp.shop.example.com), or a host the catalog declares. A bare registered domain (example.com) is
    never a sibling parent."""
    host, base = host.lower().rstrip("."), base.lower().rstrip(".")
    if host in {a.lower() for a in allowed}:
        return True
    if host == base or host.endswith("." + base) or base.endswith("." + host):
        return True
    hl, bl = host.split("."), base.split(".")
    common = 0
    for a, b in zip(reversed(hl), reversed(bl), strict=False):
        if a != b:
            break
        common += 1
    return common >= 3


def _build(httpx_mod: Any, httpcore_mod: Any, auto_backend: type, too_large: type[Exception]
           ) -> tuple[type, type]:
    """The guarded backend and transport for one HTTP library (httpx/httpcore or httpx2/httpcore2)."""

    class Backend(httpcore_mod.AsyncNetworkBackend):
        """Resolves, validates and connects in one step: the address that was checked is the one used."""

        def __init__(self) -> None:
            self._inner = auto_backend()

        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None  # noqa: ASYNC109
        ):
            last: Exception | None = None
            for ip in await resolve_public(host, port):
                try:
                    return await self._inner.connect_tcp(
                        ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
                except httpcore_mod.ConnectError as exc:
                    last = exc
            raise last or httpcore_mod.ConnectError("no address")

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # noqa: ASYNC109
            raise UnsafeUrl("unix sockets are not allowed")

        async def sleep(self, seconds: float) -> None:
            await self._inner.sleep(seconds)

    class Capped(httpx_mod.AsyncByteStream):
        def __init__(self, inner: Any, limit: int) -> None:
            self._inner, self._limit = inner, limit

        async def __aiter__(self) -> AsyncIterator[bytes]:
            total = 0
            async for chunk in self._inner:
                total += len(chunk)
                if total > self._limit:
                    await self._inner.aclose()
                    raise too_large("response too large")
                yield chunk

        async def aclose(self) -> None:
            await self._inner.aclose()

    class Transport(httpx_mod.AsyncHTTPTransport):
        def __init__(self, *, max_bytes: int = MAX_BYTES) -> None:
            super().__init__(retries=0)
            self._pool._network_backend = Backend()  # noqa: SLF001 (httpx exposes no hook for this)
            self._max_bytes = max_bytes

        async def handle_async_request(self, request: Any) -> Any:
            await check_url(str(request.url))
            limit = int(request.extensions.get("max_bytes", self._max_bytes))
            resp = await super().handle_async_request(request)
            if 300 <= resp.status_code < 400:
                await resp.aclose()
                raise UnsafeUrl("redirects are not followed")
            try:
                declared = int(resp.headers.get("content-length", "0"))
            except ValueError:
                declared = 0
            if declared > limit:
                await resp.aclose()
                raise too_large("response too large")
            return httpx_mod.Response(resp.status_code, headers=resp.headers,
                                      stream=Capped(resp.stream, limit), extensions=resp.extensions)

    return Backend, Transport


GuardedBackend, GuardedTransport = _build(httpx, httpcore, AutoBackend, ResponseTooLarge)
McpBackend, McpTransport = _build(httpx2, httpcore2, AutoBackend2, SdkResponseTooLarge)


def guarded_client(*, max_bytes: int = MAX_BYTES, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
    """The HTTP client for our own OAuth and discovery calls: guarded transport, no redirects, bounded
    timeouts."""
    return httpx.AsyncClient(transport=GuardedTransport(max_bytes=max_bytes), follow_redirects=False,
                             timeout=timeout or TIMEOUT)


def guarded_mcp_client(
    headers: Mapping[str, str] | None = None, timeout: httpx2.Timeout | None = None, auth: Any = None, *,
    max_bytes: int = MCP_BYTES, event_hooks: Mapping[str, list[Any]] | None = None,
) -> httpx2.AsyncClient:
    """The client handed to the `mcp` SDK (an httpx2 client). The signature starts with the SDK's
    `httpx_client_factory` arguments (headers, timeout, auth), so it can be used as that factory too."""
    kwargs: dict[str, Any] = {}
    if headers:
        kwargs["headers"] = dict(headers)
    if auth is not None:
        kwargs["auth"] = auth
    if event_hooks:
        kwargs["event_hooks"] = {k: list(v) for k, v in event_hooks.items()}
    return httpx2.AsyncClient(transport=McpTransport(max_bytes=max_bytes), follow_redirects=False,
                              timeout=timeout or MCP_TIMEOUT, **kwargs)
