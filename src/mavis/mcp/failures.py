# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous
# Research, MIT License.
"""What went wrong talking to an MCP server, in three kinds and a short code.

Adapted from Hermes Agent: `tools/mcp_tool_errors.py` (`_unwrap_exception_group`, `_classify_mcp_failure`,
`_is_streamable_http_rejection`, `_SESSION_EXPIRED_MARKERS`, `_is_session_expired_error`) and the at-most-once
rule of `tools/mcp_tool_handlers.py` (a write is never replayed after a session-expired or transport error).

The `mcp` SDK folds every non-2xx answer into an opaque `MCPError(-32603, "Server returned an error
response")` and wraps errors in anyio exception groups, so the HTTP status is recorded on the side (see
`HttpProbe`) and passed in. Nothing here ever carries a response body, a header, a URL query or a token: the
code is a fixed word and the detail is sanitized and short."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

import httpx2
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from mavis.connectors.netguard import TOO_LARGE, UnsafeUrl
from mavis.mcp.text import clean_description, sanitize_error

JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603
# parse error, invalid request, method not found, invalid params: the server refused before running anything
SERVER_REFUSAL_CODES = frozenset({-32700, -32600, -32601, -32602})
# What an SSE-only server (or its load balancer) answers the Streamable HTTP initialize POST with: Bad
# Request, Method Not Allowed, Not Acceptable, Length Required.
STREAMABLE_REJECT_STATUSES = (400, 405, 406, 411)

# Lower-cased substrings meaning the transport session expired or the stream was closed under us.
SESSION_MARKERS = (
    "invalid or expired session", "expired session", "session expired", "session not found",
    "unknown session", "session terminated", "closedresourceerror", "closed resource",
    "transport is closed", "connection closed", "broken pipe", "end of file")


class Kind(StrEnum):
    AUTH = "auth"  # 401/403: the connection needs the user (reconnect), never retried
    PERMANENT = "permanent"  # not an MCP endpoint, a bad URL, a 404: retrying cannot help
    TRANSIENT = "transient"  # timeouts, 5xx, resets: may work next time


@dataclass(frozen=True)
class Failure:
    kind: Kind
    code: str  # a fixed short word, safe to store (mcp_servers.last_error) and to show
    unknown_outcome: bool = False  # for a tool call: the request may have been executed


class McpFailure(Exception):
    """A failed MCP operation, already classified. `str()` is the code; `detail` is sanitized text."""

    def __init__(self, failure: Failure, detail: str = "", *, www_authenticate: str | None = None) -> None:
        super().__init__(failure.code)
        # `www_authenticate`: the 401's challenge header (its `resource_metadata` pointer starts discovery)
        self.failure, self.detail, self.www_authenticate = failure, detail, www_authenticate

    @property
    def kind(self) -> Kind:
        return self.failure.kind

    @property
    def code(self) -> str:
        return self.failure.code


class HttpProbe:
    """Remembers the status of the last POST the server answered (the JSON-RPC requests), when it was an
    error, plus the WWW-Authenticate header; never a body. Installed as an httpx2 response hook, because the
    SDK folds the status into an opaque error. A successful POST clears it, and GET and DELETE answers are
    ignored (a server that has no event stream answers the SDK's GET with 405 while working fine)."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.www_authenticate: str | None = None

    def reset(self) -> None:
        self.status, self.www_authenticate = None, None

    async def __call__(self, response: httpx2.Response) -> None:
        if response.request.method != "POST":
            return
        if response.status_code >= 400:
            self.status = response.status_code
            self.www_authenticate = response.headers.get("www-authenticate")
        else:
            self.reset()


def _only_cancellation(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return all(_only_cancellation(sub) for sub in exc.exceptions)
    return isinstance(exc, asyncio.CancelledError)


def unwrap_exception_group(exc: BaseException) -> BaseException:
    """The root-cause leaf of anyio's (Base)ExceptionGroup wrappers (a group's `str()` is opaque). A
    KeyboardInterrupt or SystemExit anywhere is re-raised, never flattened into an error; a real error is
    preferred over the CancelledError noise anyio sprays on siblings."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        leaf = exc.split((KeyboardInterrupt, SystemExit))[0]
        if leaf is not None:
            while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
                leaf = leaf.exceptions[0]
            raise leaf
        exc = next((sub for sub in exc.exceptions if not _only_cancellation(sub)), exc.exceptions[0])
    return exc


def _walk(exc: BaseException):
    seen: set[int] = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        for nxt in (cur.__cause__, cur.__context__):
            if nxt is not None:
                stack.append(nxt)


def is_session_expired(exc: BaseException) -> bool:
    """The transport session expired or its stream was closed (the access token is still fine)."""
    import anyio

    transport_types = (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream)
    for cur in _walk(exc):
        if isinstance(cur, InterruptedError):
            return False
    return any(isinstance(cur, transport_types) or any(m in str(cur).lower() for m in SESSION_MARKERS)
               for cur in _walk(exc))


def is_streamable_rejection(exc: BaseException, status: int | None) -> bool:
    """A Streamable HTTP connect failure that looks like a transport mismatch rather than a broken server: a
    400-family rejection of the initialize POST. Timeouts and auth errors never qualify, so a slow or 401ing
    server is not retried on the wrong transport. With no recorded status, the SDK's opaque -32603 counts."""
    if status is not None:
        return status in STREAMABLE_REJECT_STATUSES
    root = unwrap_exception_group(exc)
    return (isinstance(root, MCPError) and root.code == JSONRPC_INTERNAL_ERROR
            and "server returned an error response" in str(root).lower())


def _by_status(status: int, phase: str) -> Failure | None:
    if status in (401, 403):
        return Failure(Kind.AUTH, "auth")
    if status == 404:
        return Failure(Kind.PERMANENT, "not_found")
    if status in (400, 405, 406, 411, 415):
        return Failure(Kind.PERMANENT, "not_mcp")
    if status == 429:
        return Failure(Kind.TRANSIENT, "rate_limited")
    if status >= 500:
        return Failure(Kind.TRANSIENT, "server_error", unknown_outcome=phase == "call")
    if status >= 400:
        return Failure(Kind.PERMANENT, "rejected")
    return None


def classify(exc: BaseException, *, status: int | None = None, phase: str = "connect") -> Failure:
    """The kind, short code and (for a call) whether the request may have run. `phase` is "connect" (probe,
    list tools, refresh) or "call" (a tool call: after the session was set up, so a transport failure leaves
    the outcome unknown)."""
    call = phase == "call"
    root = unwrap_exception_group(exc)
    if isinstance(root, McpFailure):
        return root.failure
    if isinstance(root, UnsafeUrl):
        return Failure(Kind.PERMANENT, "redirect" if "redirect" in str(root) else "blocked_url")
    if isinstance(root, TOO_LARGE):
        return Failure(Kind.TRANSIENT if call else Kind.PERMANENT, "too_large", unknown_outcome=call)
    if status is None:  # a status the SDK kept on its HTTPStatusError (the SSE transport raises for status)
        status = getattr(getattr(root, "response", None), "status_code", None)
    if status is not None and (found := _by_status(status, phase)) is not None:
        return found
    if isinstance(root, (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)):
        return Failure(Kind.TRANSIENT, "unreachable")  # nothing was sent
    if isinstance(root, (httpx2.TimeoutException, TimeoutError)):
        return Failure(Kind.TRANSIENT, "timeout", unknown_outcome=call)
    if isinstance(root, MCPError):
        if root.code == JSONRPC_METHOD_NOT_FOUND and not call:
            return Failure(Kind.PERMANENT, "not_mcp")
        # Only an error the server itself returned for a malformed request proves the call did not run. The
        # SDK also raises MCPError for a closed connection and a request timeout; those, and -32603 or any
        # unknown code, may have followed a call that ran.
        sent = root.code in SERVER_REFUSAL_CODES
        return Failure(Kind.TRANSIENT, "rpc_error" if call else "protocol",
                       unknown_outcome=call and not sent)
    if isinstance(root, (ValidationError, ValueError, KeyError)):
        return Failure(Kind.TRANSIENT if call else Kind.PERMANENT, "protocol")
    if is_session_expired(exc):
        return Failure(Kind.TRANSIENT, "session", unknown_outcome=call)
    if isinstance(root, (httpx2.TransportError, OSError)):
        return Failure(Kind.TRANSIENT, "unreachable" if not call else "connection_lost", unknown_outcome=call)
    return Failure(Kind.TRANSIENT, "unknown", unknown_outcome=call)


def safe_detail(exc: BaseException, secrets: tuple[str, ...] = (), limit: int = 200) -> str:
    """The root cause as a short sanitized sentence for a model: type name and message, credentials blanked,
    no URL queries. Only used for errors a server itself reported (a JSON-RPC error message)."""
    root = unwrap_exception_group(exc)
    message = str(root.message if isinstance(root, MCPError) else root).strip()
    text = sanitize_error(f"{type(root).__name__}: {message}" if message else type(root).__name__, secrets)
    return clean_description(text, limit)
