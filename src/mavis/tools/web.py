"""Web search (Tavily, DuckDuckGo fallback) and page extraction.

Everything returned from here is third-party content: the tools are registered with
``untrusted_output=True`` so the registry wraps results before the model sees them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html import unescape
from urllib.parse import urljoin, urlparse

import httpcore
import httpx
import structlog
from pydantic import BaseModel, Field

from mavis.config import get_settings
from mavis.domain.policy import Capability, RiskClass
from mavis.tools.registry import MavisTool

log = structlog.get_logger()

TAVILY_URL = "https://api.tavily.com"
_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_SPACE = re.compile(r"\s+")
_MAX_PAGE_CHARS = 20000
_MAX_BODY_BYTES = 2_000_000  # hard cap on bytes read from any page
_MAX_REDIRECTS = 4
_FETCH_TIMEOUT_S = 15.0
_FETCH_DEADLINE_S = 25.0  # total wall clock for one direct fetch, redirects included


class SearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=400, description="Focused search query")
    max_results: int = Field(default=5, ge=1, le=10)


class ExtractArgs(BaseModel):
    url: str = Field(pattern=r"^https?://", description="Absolute http(s) URL to read")


async def tavily_search(query: str, max_results: int) -> list[dict]:
    key = get_settings().tavily_api_key
    if not key:
        raise RuntimeError("TAVILY_API_KEY not set")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{TAVILY_URL}/search",
            json={
                "query": query, "max_results": max_results,
                "search_depth": "basic", "include_answer": True,
            },
            headers={"Authorization": f"Bearer {key}"},
        )
        resp.raise_for_status()
        data = resp.json()
    rows = [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("content") or "")[:500]}
        for r in data.get("results", [])
    ]
    if data.get("answer"):
        rows.insert(0, {"title": "Summary", "url": "", "snippet": str(data["answer"])[:500]})
    return rows


async def ddg_search(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    def _run() -> list[dict]:
        return list(DDGS().text(query, max_results=max_results))

    try:
        rows = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001 - ddgs raises when there are no results
        if "no results" in str(exc).lower():
            return []
        raise
    return [
        {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": (r.get("body") or "")[:500]}
        for r in rows
    ]


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str


async def search(query: str, max_results: int = 5) -> list[SearchHit]:
    """Public helper (Phase 6 DeepResearch): Tavily, falling back to DuckDuckGo."""
    try:
        rows = await tavily_search(query, max_results)
    except Exception as exc:  # noqa: BLE001 - any Tavily failure falls back
        log.warning("web.tavily_failed", error=type(exc).__name__)
        rows = await ddg_search(query, max_results)
    return [SearchHit(**r) for r in rows]


async def web_search(user_id: int, args: SearchArgs) -> str:
    rows = [h.model_dump() for h in await search(args.query, args.max_results)]
    if not rows:
        return "No results."
    return "\n".join(f"[{i}] {r['title']}: {r['url']}\n{r['snippet']}" for i, r in enumerate(rows, start=1))


# ---------------------------------------------------------------- SSRF guard

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def _check_ip(raw: str) -> None:
    """Raise ValueError unless ``raw`` is a globally routable unicast address."""
    ip = ipaddress.ip_address(raw.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        ip = ip.ipv4_mapped or ip
    if isinstance(ip, ipaddress.IPv6Address):
        # Embedded IPv4 (NAT64, 6to4) must itself be public.
        if ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip in _SIX_TO_FOUR:
            ip = ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    if not ip.is_global or ip.is_multicast:
        raise ValueError(f"refusing to fetch non-public address {ip}")


async def _resolve_public(host: str, port: int) -> list[str]:
    """Resolve ``host`` and return its addresses, raising ValueError if ANY is non-public."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve host: {host}") from exc
    addrs = list(dict.fromkeys(info[4][0] for info in infos))
    if not addrs:
        raise ValueError(f"cannot resolve host: {host}")
    for addr in addrs:
        _check_ip(addr)
    return addrs


async def assert_public_url(url: str) -> None:
    """Refuse URLs that are not http(s) or that resolve to non-public addresses (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only absolute http(s) URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("credentials in URLs are not allowed")
    try:
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
    except ValueError as exc:
        raise ValueError("invalid port") from exc
    await _resolve_public(parsed.hostname, port)


class _PinnedBackend(httpcore.AnyIOBackend):
    """Network backend that resolves, validates and connects in one step.

    The address that was validated is the address connected to, so a DNS answer that
    changes between a pre-check and the connection (rebinding) cannot reach a private host.
    TLS still verifies the certificate against the original hostname.
    """

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore interface
        last: Exception | None = None
        for addr in await _resolve_public(host, port):
            try:
                return await super().connect_tcp(
                    addr, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except httpcore.ConnectError as exc:
                last = exc
        raise last or httpcore.ConnectError(f"cannot connect to {host}")


class _AsyncResponseStream(httpx.AsyncByteStream):
    def __init__(self, httpcore_stream) -> None:
        self._stream = httpcore_stream

    async def __aiter__(self):
        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedTransport(httpx.AsyncBaseTransport):
    """httpx transport over a public-API httpcore pool that uses the pinned backend."""

    def __init__(self) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=_PinnedBackend(), max_connections=10, retries=0
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme, host=request.url.raw_host,
                port=request.url.port, target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        resp = await self._pool.handle_async_request(req)
        return httpx.Response(
            status_code=resp.status, headers=resp.headers,
            stream=_AsyncResponseStream(resp.stream), extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def _pinned_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_PinnedTransport(), timeout=_FETCH_TIMEOUT_S, follow_redirects=False, trust_env=False
    )


async def _guarded_get(url: str) -> str:
    """GET with per-hop validation of redirects, a size cap and an overall deadline."""
    async with _pinned_client() as client:
        for _ in range(_MAX_REDIRECTS + 1):
            await assert_public_url(url)
            headers = {"User-Agent": "Mavis/0.1", "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"}
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError("redirect without a location")
                    url = urljoin(url, location)
                    continue
                resp.raise_for_status()
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= _MAX_BODY_BYTES:
                        break
                return bytes(body[:_MAX_BODY_BYTES]).decode(resp.encoding or "utf-8", errors="replace")
        raise ValueError("too many redirects")


def html_to_text(html: str) -> str:
    return _SPACE.sub(" ", unescape(_TAGS.sub(" ", html))).strip()


async def _tavily_extract(url: str) -> str:
    key = get_settings().tavily_api_key
    if not key:
        raise RuntimeError("TAVILY_API_KEY not set")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TAVILY_URL}/extract", json={"urls": [url]}, headers={"Authorization": f"Bearer {key}"}
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    if not results:
        raise RuntimeError("tavily extract returned nothing")
    return str(results[0].get("raw_content") or "")


async def extract(url: str, max_chars: int = _MAX_PAGE_CHARS) -> str:
    """Public helper (Phase 6 DeepResearch): SSRF-guarded page text, Tavily first, plain GET fallback."""
    await assert_public_url(url)
    try:
        text = await _tavily_extract(url)
    except Exception as exc:  # noqa: BLE001
        log.info("web.extract_fallback", error=type(exc).__name__)
        async with asyncio.timeout(_FETCH_DEADLINE_S):
            text = html_to_text(await _guarded_get(url))
    return text[:max_chars]


async def web_extract(user_id: int, args: ExtractArgs) -> str:
    return await extract(args.url)


TOOLS = [
    MavisTool(
        name="web_search",
        description="Search the web. Returns numbered results with title, URL and snippet.",
        args_model=SearchArgs, risk=RiskClass.READ, fn=web_search, requires=Capability.WEB,
        agents=frozenset({"conversation", "research", "knowledge", "spawn"}),
        untrusted_output=True, priority=70,
    ),
    MavisTool(
        name="web_extract",
        description="Read the main text of a web page by URL.",
        args_model=ExtractArgs, risk=RiskClass.READ, fn=web_extract, requires=Capability.WEB,
        # Not "conversation": chat reads mail and calendar, so a model-chosen URL fetch there could
        # carry private data out in a query string. Research runs on tainted-aware task loops.
        agents=frozenset({"research", "spawn"}),
        untrusted_output=True, priority=40,
    ),
]
