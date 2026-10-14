"""A small fake MCP server for tests: JSON-RPC over Streamable HTTP (POST /mcp) and the old SSE transport
(GET /sse + POST /messages), with knobs for every failure the client must survive. It records what it was
asked, so tests can assert on the wire (the Authorization header, the arguments)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

PROTOCOL = "2025-06-18"


def tool(name: str, description: str = "", properties: dict | None = None, required: list[str] | None = None,
         read_only: bool | None = None, destructive: bool | None = None) -> dict:
    """A `tools/list` entry."""
    entry: dict[str, Any] = {
        "name": name, "description": description or f"The {name} tool.",
        "inputSchema": {"type": "object", "properties": properties or {}, "required": required or []}}
    pairs = (("readOnlyHint", read_only), ("destructiveHint", destructive))
    hints = {k: v for k, v in pairs if v is not None}
    if hints:
        entry["annotations"] = hints
    return entry


def text_result(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class Truncated(Response):
    """Promise a long body, send a piece, and stop: the client sees the connection die mid-answer."""

    async def __call__(self, scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/json"), (b"content-length", b"4000")]})
        await send({"type": "http.response.body", "body": b'{"jsonrpc":"2.0","id":', "more_body": True})


@dataclass
class FakeMcpServer:
    tools: list[dict] = field(default_factory=list)
    handlers: dict[str, Callable[[dict], dict | Response]] = field(default_factory=dict)
    token: str | None = None  # when set, every request must carry "Authorization: Bearer <token>"
    resource_metadata: str = ""  # advertised in the 401's WWW-Authenticate
    page_size: int = 0  # >0: tools/list returns this many per page, with nextCursor
    always_status: int | None = None  # answer every request with this status
    streamable_status: int | None = None  # answer only POST /mcp with this status (an SSE-only server)
    sse_responses: bool = False  # answer POSTs as text/event-stream
    delay_s: float = 0.0  # sleep before answering tools/call
    list_delay_s: float = 0.0  # sleep before answering tools/list
    fail_next_calls: int = 0  # answer this many tools/call requests with a 503, then work
    attempts: int = 0  # tools/call requests seen (counted even when the answer is a failure)
    in_flight: int = 0
    max_in_flight: int = 0
    hang_initialize: bool = False  # never answer initialize (the connection just sits open)
    drop_calls: bool = False  # cut the connection during tools/call
    capabilities: dict = field(default_factory=lambda: {"tools": {}})
    prm: dict | None = None  # the RFC 9728 protected-resource metadata this server publishes
    requests: list[dict] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    _sse_queues: dict[str, asyncio.Queue] = field(default_factory=dict)

    # --- JSON-RPC ---------------------------------------------------------------------------------

    def _unauthorized(self, request: Request) -> Response | None:
        if self.token is None or request.headers.get("authorization") == f"Bearer {self.token}":
            return None
        return Response(status_code=401, content=b"nope secret body token=hunter2hunter2", headers={
            "www-authenticate": f'Bearer resource_metadata="{self.resource_metadata}"'})

    def _result(self, body: dict) -> dict | Response:
        method, id_, params = body.get("method"), body.get("id"), body.get("params") or {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": PROTOCOL, "capabilities": self.capabilities,
                "serverInfo": {"name": "fake", "version": "1"}}}
        if method == "tools/list":
            start = int(params.get("cursor") or 0)
            page = self.tools[start:start + self.page_size] if self.page_size else self.tools
            more = self.page_size and start + self.page_size < len(self.tools)
            result: dict[str, Any] = {"tools": page}
            if more:
                result["nextCursor"] = str(start + self.page_size)
            return {"jsonrpc": "2.0", "id": id_, "result": result}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            self.calls.append((name, args))
            handler = self.handlers.get(name)
            if handler is None:
                error = {"code": -32602, "message": f"Unknown tool: {name}"}
                return {"jsonrpc": "2.0", "id": id_, "error": error}
            out = handler(args)
            if isinstance(out, Response):
                return out
            return {"jsonrpc": "2.0", "id": id_, "result": out}
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "Method not found"}}

    async def _handle(self, body: dict) -> dict | Response | None:
        if body.get("method", "").startswith("notifications/"):
            return None
        if body.get("method") == "initialize" and self.hang_initialize:
            await asyncio.sleep(3600)
        if body.get("method") == "tools/list" and self.list_delay_s:
            await asyncio.sleep(self.list_delay_s)
        if body.get("method") == "tools/call":
            self.attempts += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                if self.drop_calls:
                    return Truncated()
                if self.fail_next_calls > 0:
                    self.fail_next_calls -= 1
                    return Response(status_code=503, content=b"busy secret=abc123abc123")
            finally:
                self.in_flight -= 1
        return self._result(body)

    # --- Streamable HTTP --------------------------------------------------------------------------

    async def mcp(self, request: Request) -> Response:
        self.requests.append({"method": request.method, "path": request.url.path,
                              "auth": request.headers.get("authorization")})
        if self.always_status:
            return Response(status_code=self.always_status, content=b"boom secret=abc123abc123")
        if request.method != "POST":
            return Response(status_code=405)
        if self.streamable_status:
            return Response(status_code=self.streamable_status)
        if (denied := self._unauthorized(request)) is not None:
            return denied
        body = await request.json()
        out = await self._handle(body)
        if out is None:
            return Response(status_code=202)
        if isinstance(out, Response):
            return out
        if self.sse_responses:
            return Response(f"event: message\ndata: {json.dumps(out)}\n\n", media_type="text/event-stream")
        return JSONResponse(out)

    async def protected_resource(self, request: Request) -> Response:
        self.requests.append({"method": "GET", "path": request.url.path, "auth": None})
        return JSONResponse(self.prm) if self.prm is not None else Response(status_code=404)

    # --- the old SSE transport --------------------------------------------------------------------

    async def sse(self, request: Request) -> Response:
        self.requests.append({"method": "GET", "path": "/sse", "auth": request.headers.get("authorization")})
        if (denied := self._unauthorized(request)) is not None:
            return denied
        sid = f"s{len(self._sse_queues) + 1}"
        queue: asyncio.Queue = asyncio.Queue()
        self._sse_queues[sid] = queue

        async def events():
            yield f"event: endpoint\ndata: /messages?session_id={sid}\n\n".encode()
            while True:
                message = await queue.get()
                yield f"event: message\ndata: {json.dumps(message)}\n\n".encode()

        return StreamingResponse(events(), media_type="text/event-stream")

    async def messages(self, request: Request) -> Response:
        self.requests.append({"method": "POST", "path": "/messages",
                              "auth": request.headers.get("authorization")})
        if (denied := self._unauthorized(request)) is not None:
            return denied
        queue = self._sse_queues.get(request.query_params.get("session_id", ""))
        if queue is None:
            return Response(status_code=404)
        out = await self._handle(await request.json())
        if isinstance(out, dict):
            await queue.put(out)
        return Response(status_code=202)

    @property
    def app(self) -> Starlette:
        return Starlette(routes=[
            Route("/mcp", self.mcp, methods=["GET", "POST", "DELETE"]),
            Route("/.well-known/oauth-protected-resource", self.protected_resource),
            Route("/.well-known/oauth-protected-resource/mcp", self.protected_resource),
            Route("/sse", self.sse, methods=["GET"]),
            Route("/messages", self.messages, methods=["POST"]),
        ])
