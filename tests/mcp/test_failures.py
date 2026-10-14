"""Failure classification (adapted from Hermes Agent) and the HTTP probe."""

from __future__ import annotations

import asyncio

import anyio
import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from mavis.connectors.netguard import ResponseTooLarge, SdkResponseTooLarge, UnsafeUrl
from mavis.mcp import failures as f
from mavis.mcp.failures import Failure, Kind, McpFailure


def group(*excs):
    return BaseExceptionGroup("unhandled errors in a TaskGroup", list(excs))


def test_exception_groups_unwrap_to_the_real_error() -> None:
    inner = ValueError("boom")
    nested = group(group(asyncio.CancelledError(), inner))
    assert f.unwrap_exception_group(nested) is inner


def test_a_keyboard_interrupt_inside_a_group_is_re_raised() -> None:
    with pytest.raises(KeyboardInterrupt):
        f.unwrap_exception_group(group(ValueError("x"), KeyboardInterrupt()))


def test_cancellation_only_group_unwraps_to_the_cancellation() -> None:
    c = asyncio.CancelledError()
    assert f.unwrap_exception_group(group(c)) is c


@pytest.mark.parametrize(("status", "kind", "code"), [
    (401, Kind.AUTH, "auth"), (403, Kind.AUTH, "auth"), (404, Kind.PERMANENT, "not_found"),
    (405, Kind.PERMANENT, "not_mcp"), (418, Kind.PERMANENT, "rejected"),
    (429, Kind.TRANSIENT, "rate_limited"),
    (500, Kind.TRANSIENT, "server_error"), (503, Kind.TRANSIENT, "server_error"),
])
def test_http_statuses_classify(status, kind, code) -> None:
    opaque = group(MCPError(f.JSONRPC_INTERNAL_ERROR, "Server returned an error response"))
    got = f.classify(opaque, status=status)
    assert (got.kind, got.code) == (kind, code)


def test_a_5xx_during_a_call_leaves_the_outcome_unknown_but_a_4xx_does_not() -> None:
    err = MCPError(f.JSONRPC_INTERNAL_ERROR, "Server returned an error response")
    assert f.classify(err, status=502, phase="call").unknown_outcome is True
    assert f.classify(err, status=400, phase="call").unknown_outcome is False
    assert f.classify(err, status=401, phase="call") == Failure(Kind.AUTH, "auth")


@pytest.mark.parametrize(("exc", "code", "unknown_in_call"), [
    (httpx2.ConnectError("nope"), "unreachable", False),
    (httpx2.ConnectTimeout("slow"), "unreachable", False),
    (httpx2.ReadTimeout("slow"), "timeout", True),
    (TimeoutError(), "timeout", True),
    (httpx2.ReadError("reset"), "connection_lost", True),
    (anyio.ClosedResourceError(), "session", True),
    (RuntimeError("Session terminated by peer"), "session", True),
])
def test_transport_errors_classify_and_calls_report_unknown_outcome(exc, code, unknown_in_call) -> None:
    connect = f.classify(group(exc))
    assert connect.kind is Kind.TRANSIENT and connect.unknown_outcome is False
    call = f.classify(group(exc), phase="call")
    assert call.unknown_outcome is unknown_in_call
    assert code in (connect.code, call.code)


def test_the_net_guard_errors_are_permanent_and_named() -> None:
    assert f.classify(UnsafeUrl("the url must be https")) == Failure(Kind.PERMANENT, "blocked_url")
    assert f.classify(UnsafeUrl("redirects are not followed")) == Failure(Kind.PERMANENT, "redirect")
    assert f.classify(SdkResponseTooLarge("big")).code == "too_large"
    assert f.classify(ResponseTooLarge("big")).kind is Kind.PERMANENT
    assert f.classify(SdkResponseTooLarge("big"), phase="call").unknown_outcome is True


def test_method_not_found_while_connecting_means_not_an_mcp_endpoint() -> None:
    assert f.classify(MCPError(f.JSONRPC_METHOD_NOT_FOUND, "Method not found")) == Failure(
        Kind.PERMANENT, "not_mcp")
    assert f.classify(MCPError(-32602, "bad params"), phase="call") == Failure(Kind.TRANSIENT, "rpc_error")


def test_a_malformed_result_is_a_protocol_error() -> None:
    assert f.classify(ValueError("not json")) == Failure(Kind.PERMANENT, "protocol")
    assert f.classify(ValueError("not json"), phase="call").kind is Kind.TRANSIENT


def test_an_already_classified_failure_passes_through() -> None:
    mine = McpFailure(Failure(Kind.AUTH, "auth"))
    assert f.classify(group(mine)) == Failure(Kind.AUTH, "auth")
    assert str(mine) == "auth" and mine.kind is Kind.AUTH


class Weird(Exception):
    pass


def test_unknown_errors_are_transient() -> None:
    assert f.classify(Weird()) == Failure(Kind.TRANSIENT, "unknown")


def test_streamable_rejection_uses_the_recorded_status() -> None:
    err = MCPError(f.JSONRPC_INTERNAL_ERROR, "Server returned an error response")
    for status in (400, 405, 406, 411):
        assert f.is_streamable_rejection(err, status) is True
    for status in (401, 403, 404, 429, 500):
        assert f.is_streamable_rejection(err, status) is False


def test_streamable_rejection_without_a_status_trusts_only_the_opaque_sdk_error() -> None:
    assert f.is_streamable_rejection(
        group(MCPError(f.JSONRPC_INTERNAL_ERROR, "Server returned an error response")), None) is True
    assert f.is_streamable_rejection(httpx2.ReadTimeout("slow"), None) is False
    assert f.is_streamable_rejection(MCPError(-32000, "other"), None) is False


def test_session_expiry_is_detected_by_type_and_by_marker_and_interruption_overrides() -> None:
    assert f.is_session_expired(anyio.BrokenResourceError()) is True
    assert f.is_session_expired(RuntimeError("Invalid or expired session")) is True
    assert f.is_session_expired(RuntimeError("something else")) is False
    chained = RuntimeError("wrapper")
    chained.__cause__ = anyio.ClosedResourceError()
    assert f.is_session_expired(chained) is True
    assert f.is_session_expired(group(RuntimeError("session expired"), InterruptedError())) is False


def test_safe_detail_blanks_credentials_and_is_short() -> None:
    exc = MCPError(-32000, "bad Bearer abc.def.ghi and token=zzz999 " + "x" * 500)
    out = f.safe_detail(exc, secrets=("zzz999",))
    assert "abc.def.ghi" not in out and "zzz999" not in out and len(out) <= 200
    assert out.startswith("MCPError:")


def _response(status: int, method: str = "POST", **kw) -> httpx2.Response:
    return httpx2.Response(status, request=httpx2.Request(method, "https://x.example/mcp"), **kw)


async def test_the_probe_remembers_the_last_post_error_status_and_the_auth_header_only() -> None:
    probe = f.HttpProbe()
    await probe(_response(200))
    assert probe.status is None
    bad = _response(401, headers={"www-authenticate": 'Bearer resource_metadata="https://x/y"'},
                    content=b"secret body token=abc")
    await probe(bad)
    assert (probe.status, probe.www_authenticate) == (401, 'Bearer resource_metadata="https://x/y"')
    assert not hasattr(probe, "body")
    probe.reset()
    assert probe.status is None and probe.www_authenticate is None


async def test_the_probe_ignores_get_answers_and_a_good_post_clears_an_earlier_error() -> None:
    probe = f.HttpProbe()
    await probe(_response(405, method="GET"))  # no event stream: harmless
    assert probe.status is None
    await probe(_response(400))  # the server did not understand the first handshake
    assert probe.status == 400
    await probe(_response(200))  # the next one worked
    assert probe.status is None


def test_only_a_server_refusal_code_leaves_a_call_outcome_known() -> None:
    for code in (-32700, -32600, -32601, -32602):
        assert f.classify(MCPError(code, "no"), phase="call").unknown_outcome is False
    for code in (-32603, -32000, -32001, 7):
        assert f.classify(MCPError(code, "no"), phase="call").unknown_outcome is True
