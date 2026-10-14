# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous
# Research, MIT License.
"""The MCP protocol client: per-call sessions over Streamable HTTP, with Hermes's SSE fallback.

Every operation (list tools, call a tool) opens a session, initializes, does the work and closes it. There
are no keepalives, no long-lived connections and no supervisor (spec D2). All HTTP goes through an httpx2
client built by the net guard (`netguard.guarded_mcp_client`): https only, public addresses only, no
redirects, a 4 MB cap per response, bounded timeouts.

Adapted from Hermes Agent `tools/mcp_tool_transport.py` (the Streamable HTTP client, the SSE fallback on a
400/405/406/411 answer to the first connect, capability-gated `tools/list`, pagination up to 50 pages) and
`tools/mcp_tool_errors.py` (the response recorder, exception-group unwrapping).

Failures leave this module as `McpFailure` (kind, short code, no bodies, no URLs, no tokens). A call that
fails after its request may have reached the server reports `unknown_outcome`: it is never retried here."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import mcp_types as mt
import structlog
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from mavis.connectors import netguard
from mavis.mcp.failures import (
    Failure,
    HttpProbe,
    Kind,
    McpFailure,
    classify,
    is_streamable_rejection,
    safe_detail,
    unwrap_exception_group,
)
from mavis.mcp.limits import MAX_PAGES
from mcp import Client

log = structlog.get_logger(__name__)

CONNECT_DEADLINE_S = 30.0  # session setup (probe, handshake) before the work starts
LIST_DEADLINE_S = 60.0  # a whole tools/list, every page
CLIENT_NAME = "mavis"


@dataclass(frozen=True)
class Endpoint:
    """Where and how to talk to one server. `bearer` goes in an Authorization header; `secrets` are the plain
    secrets of the row, so error text can blank them."""

    url: str
    bearer: str | None = None
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawTool:
    """A tool as the server listed it, before the cache cleans it."""

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None


def _raw_tool(tool: mt.Tool) -> RawTool:
    ann = tool.annotations
    return RawTool(
        name=tool.name, description=tool.description or "", input_schema=tool.input_schema or {},
        read_only_hint=getattr(ann, "read_only_hint", None),
        destructive_hint=getattr(ann, "destructive_hint", None))


def _only_cancellation(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return all(_only_cancellation(sub) for sub in exc.exceptions)
    return isinstance(exc, asyncio.CancelledError)


class McpClient:
    """Stateless: one instance serves every user and server. Nothing is kept between calls."""

    def __init__(self, *, connect_deadline_s: float = CONNECT_DEADLINE_S,
                 list_deadline_s: float = LIST_DEADLINE_S) -> None:
        self.connect_deadline_s = connect_deadline_s
        self.list_deadline_s = list_deadline_s

    # --- public operations ------------------------------------------------------------------------

    async def list_tools(self, ep: Endpoint) -> list[RawTool]:
        """Every tool the server lists (up to 50 pages). Empty when the server does not advertise tools."""
        async def work(client: Client) -> list[RawTool]:
            if getattr(client.server_capabilities, "tools", None) is None:
                return []  # a prompts- or resources-only server answers tools/list with method-not-found
            tools: list[RawTool] = []
            cursor: str | None = None
            for _ in range(MAX_PAGES):
                page = await client.list_tools(cursor=cursor)
                tools.extend(_raw_tool(t) for t in page.tools)
                cursor = page.next_cursor
                if not isinstance(cursor, str) or not cursor:
                    return tools
            log.warning("mcp.list_pages_exceeded", pages=MAX_PAGES, tools=len(tools))
            return tools

        return await self._run(ep, work, phase="connect", deadline_s=self.list_deadline_s)

    async def call_tool(self, ep: Endpoint, name: str, arguments: dict[str, Any], *,
                        deadline_s: float) -> mt.CallToolResult:
        """One tool call in its own session. Never retried here."""
        async def work(client: Client) -> mt.CallToolResult:
            return await client.call_tool(name, arguments)

        return await self._run(ep, work, phase="call", deadline_s=deadline_s)

    # --- internals --------------------------------------------------------------------------------

    async def _run(self, ep: Endpoint, work, *, phase: str, deadline_s: float):
        probe = HttpProbe()
        stage = "connect"
        try:
            async with asyncio.timeout(deadline_s):
                async with self._connected(ep, probe) as client:
                    stage = phase  # the session is up: from here a transport failure leaves a call unknown
                    probe.reset()
                    return await work(client)
        except McpFailure:
            raise
        except asyncio.CancelledError:
            raise
        except BaseExceptionGroup as eg:
            if _only_cancellation(eg):
                raise
            raise self._failure(eg, probe, stage, ep) from None
        except Exception as exc:  # noqa: BLE001 - classified, never leaked
            raise self._failure(exc, probe, stage, ep) from None

    def _failure(self, exc: BaseException, probe: HttpProbe, stage: str, ep: Endpoint) -> McpFailure:
        root = unwrap_exception_group(exc)
        if isinstance(root, McpFailure):
            return root
        failure = classify(exc, status=probe.status, phase="call" if stage == "call" else "connect")
        detail = ""
        if failure.code in ("rpc_error", "protocol"):
            detail = safe_detail(exc, ep.secrets)
        log.info("mcp.failed", code=failure.code, kind=failure.kind.value, stage=stage, status=probe.status)
        return McpFailure(failure, detail,
                          www_authenticate=probe.www_authenticate if failure.kind is Kind.AUTH else None)

    @asynccontextmanager
    async def _connected(self, ep: Endpoint, probe: HttpProbe) -> AsyncIterator[Client]:
        """An initialized client session: Streamable HTTP first; on the first connect only, an answer of 400,
        405, 406 or 411 means an SSE-only server, so the SSE transport is tried."""
        headers = {"Authorization": f"Bearer {ep.bearer}"} if ep.bearer else {}
        factory = functools.partial(netguard.guarded_mcp_client, event_hooks={"response": [probe]})
        async with AsyncExitStack() as outer:
            client: Client | None = None
            try:
                async with AsyncExitStack() as attempt:
                    http = await attempt.enter_async_context(factory(headers=headers))
                    transport = streamable_http_client(ep.url, http_client=http)
                    client = await asyncio.wait_for(
                        attempt.enter_async_context(self._client(transport, "auto")), self.connect_deadline_s)
                    outer.push_async_exit(attempt.pop_all())
            except BaseException as exc:
                if client is not None or isinstance(exc, asyncio.CancelledError) \
                        or not is_streamable_rejection(exc, probe.status):
                    raise
                log.info("mcp.sse_fallback", status=probe.status)
                probe.reset()
                transport = sse_client(
                    ep.url, headers=headers, timeout=netguard.MCP_TIMEOUT.connect,
                    sse_read_timeout=netguard.MCP_TIMEOUT.read, httpx_client_factory=factory)
                client = await asyncio.wait_for(
                    outer.enter_async_context(self._client(transport, "legacy")), self.connect_deadline_s)
            yield client

    @staticmethod
    def _client(transport: Any, mode: str) -> Client:
        return Client(transport, mode=mode, cache=None, client_info=mt.Implementation(
            name=CLIENT_NAME, version="1"), read_timeout_seconds=netguard.MCP_TIMEOUT.read)


def failure_of(code: str, kind: Kind = Kind.TRANSIENT) -> McpFailure:
    """A classified failure raised by our own code (for example: no usable token)."""
    return McpFailure(Failure(kind, code))
