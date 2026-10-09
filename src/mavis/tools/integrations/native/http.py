"""Shared httpx plumbing for the native executors and the OAuth/token code.

One client per process: bounded timeouts and pool, no redirects (a vendor API never needs one and a
redirect would carry the bearer token elsewhere). `send_capped` reads a response in chunks and stops at a
byte cap, so a hostile or broken answer cannot exhaust memory.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)
TOKEN_BYTES = 64 * 1024  # token, userinfo and revoke answers
API_BYTES = 8 * 1024 * 1024  # default cap for vendor API answers


class ResponseTooLarge(Exception):
    """The vendor answer passed the byte cap. Carries no body."""


def make_client(*, timeout: httpx.Timeout | float | None = None,
                transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT if timeout is None else timeout, limits=DEFAULT_LIMITS,
        follow_redirects=False, transport=transport,
        headers={"User-Agent": "mavis-ai/1.0"},
    )


async def send_capped(client: httpx.AsyncClient, method: str, url: str, *, max_bytes: int = API_BYTES,
                      **kw: Any) -> httpx.Response:
    """Send one request and return a fully read response, raising ResponseTooLarge past `max_bytes`."""
    async with client.stream(method, url, **kw) as resp:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ResponseTooLarge
        headers = [(k, v) for k, v in resp.headers.multi_items()
                   if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(resp.status_code, headers=headers, content=bytes(body), request=resp.request)


def retry_after_s(response: httpx.Response, default: float = 30.0) -> float:
    try:
        return max(0.0, float(response.headers.get("retry-after", default)))
    except ValueError:
        return default
