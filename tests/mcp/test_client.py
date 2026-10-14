"""The MCP protocol client against a fake server on localhost: sessions, pagination, SSE fallback, auth,
failures and the response cap."""

from __future__ import annotations

import asyncio

import pytest

from mavis.mcp.client import Endpoint, McpClient
from mavis.mcp.failures import Kind, McpFailure
from tests.mcp.fake_server import FakeMcpServer, text_result, tool
from tests.mcp.localserver import serve


@pytest.fixture
def client() -> McpClient:
    return McpClient(connect_deadline_s=5, list_deadline_s=10)


def three_tools() -> list[dict]:
    return [tool("get_note", "Read a note", {"id": {"type": "string"}}, ["id"], read_only=True),
            tool("create_note", "Make a note", {"title": {"type": "string"}}),
            tool("delete_note", "Remove it", {"id": {"type": "string"}}, destructive=True)]


async def test_lists_tools_with_their_hints(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=three_tools())
    async with serve(fake.app) as base:
        tools = await client.list_tools(Endpoint(f"{base}/mcp"))
    assert [t.name for t in tools] == ["get_note", "create_note", "delete_note"]
    assert tools[0].read_only_hint is True and tools[0].destructive_hint is None
    assert tools[2].destructive_hint is True
    assert tools[0].input_schema["properties"] == {"id": {"type": "string"}}


async def test_pagination_follows_the_cursor(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=[tool(f"t{i:02d}") for i in range(7)], page_size=3)
    async with serve(fake.app) as base:
        tools = await client.list_tools(Endpoint(f"{base}/mcp"))
    assert [t.name for t in tools] == [f"t{i:02d}" for i in range(7)]
    assert sum(1 for r in fake.requests if r["method"] == "POST") >= 5  # handshake plus three pages


async def test_pagination_stops_after_fifty_pages(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=[tool(f"t{i:03d}") for i in range(80)], page_size=1)
    async with serve(fake.app) as base:
        tools = await client.list_tools(Endpoint(f"{base}/mcp"))
    assert len(tools) == 50


async def test_a_server_without_the_tools_capability_lists_nothing(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=three_tools(), capabilities={"prompts": {}})
    async with serve(fake.app) as base:
        assert await client.list_tools(Endpoint(f"{base}/mcp")) == []
    assert not any(c for c in fake.calls)


async def test_a_call_returns_the_servers_result(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=three_tools(), handlers={"get_note": lambda a: text_result(f"note {a['id']}")})
    async with serve(fake.app) as base:
        result = await client.call_tool(Endpoint(f"{base}/mcp"), "get_note", {"id": "7"}, deadline_s=10)
    assert result.content[0].text == "note 7" and not result.is_error
    assert fake.calls == [("get_note", {"id": "7"})]


async def test_a_tool_error_is_a_result_not_an_exception(localhost_ok, client) -> None:
    fake = FakeMcpServer(handlers={"get_note": lambda a: text_result("no such note", is_error=True)})
    async with serve(fake.app) as base:
        result = await client.call_tool(Endpoint(f"{base}/mcp"), "get_note", {}, deadline_s=10)
    assert result.is_error and result.content[0].text == "no such note"


async def test_responses_framed_as_server_sent_events_work(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=three_tools(), sse_responses=True,
                         handlers={"get_note": lambda a: text_result("framed")})
    async with serve(fake.app) as base:
        assert len(await client.list_tools(Endpoint(f"{base}/mcp"))) == 3
        result = await client.call_tool(Endpoint(f"{base}/mcp"), "get_note", {}, deadline_s=10)
    assert result.content[0].text == "framed"


async def test_the_bearer_token_is_sent_and_a_wrong_one_is_an_auth_failure(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=three_tools(), token="s3cr3t-token",
                         resource_metadata="https://x.example/.well-known/oauth-protected-resource")
    async with serve(fake.app) as base:
        assert len(await client.list_tools(Endpoint(f"{base}/mcp", bearer="s3cr3t-token"))) == 3
        assert fake.requests[0]["auth"] == "Bearer s3cr3t-token"
        for bearer in (None, "wrong"):
            with pytest.raises(McpFailure) as err:
                await client.list_tools(Endpoint(f"{base}/mcp", bearer=bearer))
            assert err.value.kind is Kind.AUTH and err.value.code == "auth"
            assert "resource_metadata" in (err.value.www_authenticate or "")


async def test_a_403_is_an_auth_failure_too(localhost_ok, client) -> None:
    fake = FakeMcpServer(always_status=403)
    async with serve(fake.app) as base:
        with pytest.raises(McpFailure) as err:
            await client.call_tool(Endpoint(f"{base}/mcp"), "x", {}, deadline_s=10)
    assert (err.value.kind, err.value.code) == (Kind.AUTH, "auth")


@pytest.mark.parametrize(("status", "kind", "code"), [
    (404, Kind.PERMANENT, "not_found"), (500, Kind.TRANSIENT, "server_error"),
    (503, Kind.TRANSIENT, "server_error"), (429, Kind.TRANSIENT, "rate_limited"),
])
async def test_http_errors_are_classified_without_leaking_the_body(localhost_ok, client, status, kind, code):
    fake = FakeMcpServer(always_status=status)
    async with serve(fake.app) as base:
        with pytest.raises(McpFailure) as err:
            await client.list_tools(Endpoint(f"{base}/mcp"))
    assert (err.value.kind, err.value.code) == (kind, code)
    assert "boom" not in repr(err.value) and "abc123" not in err.value.detail and err.value.detail == ""


async def test_an_sse_only_server_is_reached_through_the_fallback(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=three_tools(), streamable_status=405,
                         handlers={"get_note": lambda a: text_result("over sse")})
    async with serve(fake.app) as base:
        tools = await client.list_tools(Endpoint(f"{base}/sse"))
        result = await client.call_tool(Endpoint(f"{base}/sse"), "get_note", {}, deadline_s=10)
    assert len(tools) == 3 and result.content[0].text == "over sse"


async def test_streamable_http_is_tried_first_at_the_mcp_path(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=three_tools(), streamable_status=405)
    async with serve(fake.app) as base:
        with pytest.raises(McpFailure) as err:  # no SSE endpoint at /mcp either
            await client.list_tools(Endpoint(f"{base}/mcp"))
    assert err.value.kind is Kind.PERMANENT
    assert any(r["method"] == "POST" and r["path"] == "/mcp" for r in fake.requests)


async def test_a_timeout_during_a_call_leaves_the_outcome_unknown(localhost_ok, client) -> None:
    fake = FakeMcpServer(delay_s=5, handlers={"slow": lambda a: text_result("late")})
    async with serve(fake.app) as base:
        with pytest.raises(McpFailure) as err:
            await client.call_tool(Endpoint(f"{base}/mcp"), "slow", {}, deadline_s=0.5)
    assert err.value.code == "timeout" and err.value.failure.unknown_outcome is True


async def test_a_connection_cut_during_a_call_leaves_the_outcome_unknown(localhost_ok, client) -> None:
    fake = FakeMcpServer(drop_calls=True, handlers={"send": lambda a: text_result("sent")})
    async with serve(fake.app) as base:
        with pytest.raises(McpFailure) as err:
            await client.call_tool(Endpoint(f"{base}/mcp"), "send", {}, deadline_s=10)
    assert err.value.kind is Kind.TRANSIENT and err.value.failure.unknown_outcome is True


async def test_nothing_listening_is_unreachable_and_the_outcome_is_known(localhost_ok, client) -> None:
    with pytest.raises(McpFailure) as err:
        await client.call_tool(Endpoint("http://127.0.0.1:9/mcp"), "x", {}, deadline_s=10)
    assert err.value.code == "unreachable" and err.value.failure.unknown_outcome is False


async def test_an_error_the_server_reports_for_a_call_is_an_rpc_failure(localhost_ok, client) -> None:
    fake = FakeMcpServer()
    async with serve(fake.app) as base:
        with pytest.raises(McpFailure) as err:
            await client.call_tool(Endpoint(f"{base}/mcp"), "ghost", {}, deadline_s=10)
    assert err.value.code == "rpc_error" and "Unknown tool" in err.value.detail


async def test_an_oversized_response_is_refused(localhost_ok, client, monkeypatch) -> None:
    from mavis.connectors import netguard

    monkeypatch.setattr(netguard, "MCP_BYTES", 2000)
    import functools
    monkeypatch.setattr(netguard, "guarded_mcp_client",
                        functools.partial(netguard.guarded_mcp_client, max_bytes=2000))
    fake = FakeMcpServer(handlers={"big": lambda a: text_result("x" * 9000)})
    async with serve(fake.app) as base:
        with pytest.raises(McpFailure) as err:
            await client.call_tool(Endpoint(f"{base}/mcp"), "big", {}, deadline_s=10)
    assert err.value.code == "too_large"


async def test_a_blocked_address_never_connects(settings, client) -> None:
    with pytest.raises(McpFailure) as err:
        await client.list_tools(Endpoint("http://127.0.0.1:9/mcp"))  # localhost is off without the setting
    assert err.value.code == "blocked_url" and err.value.kind is Kind.PERMANENT


async def test_cancellation_is_not_swallowed(localhost_ok, client) -> None:
    fake = FakeMcpServer(delay_s=5, handlers={"slow": lambda a: text_result("late")})
    async with serve(fake.app) as base:
        task = asyncio.create_task(client.call_tool(Endpoint(f"{base}/mcp"), "slow", {}, deadline_s=30))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_no_session_is_kept_between_calls(localhost_ok, client) -> None:
    fake = FakeMcpServer(tools=three_tools())
    async with serve(fake.app) as base:
        await client.list_tools(Endpoint(f"{base}/mcp"))
        first = len(fake.requests)
        await client.list_tools(Endpoint(f"{base}/mcp"))
    # each list repeats the handshake: initialize, initialized, tools/list (plus a discover probe and a GET)
    assert len(fake.requests) >= 2 * first - 1


async def test_a_malformed_reply_never_puts_the_server_text_in_the_error(localhost_ok, client) -> None:
    bad = {"content": "LEAKMARKER-9f3", "isError": "LEAKMARKER-9f3"}
    fake = FakeMcpServer(handlers={"bad": lambda a: bad})
    async with serve(fake.app) as base:
        with pytest.raises(McpFailure) as err:
            await client.call_tool(Endpoint(f"{base}/mcp"), "bad", {}, deadline_s=10)
    assert err.value.code == "protocol"
    assert "LEAKMARKER" not in err.value.detail and "LEAKMARKER" not in repr(err.value)
    assert "LEAKMARKER" not in str(err.value)


async def test_a_timeout_mid_initialize_leaks_no_tasks(localhost_ok, client) -> None:
    """The client tasks (SDK transport, task group, reader) must all be gone. The test server's
    own handler for the never-answered request is not the client's and is ignored."""
    def client_tasks() -> set[asyncio.Task]:
        ours = {"RequestResponseCycle.run_asgi", "Server.serve"}
        return {t for t in asyncio.all_tasks() if t.get_coro().__qualname__ not in ours}

    fake = FakeMcpServer(hang_initialize=True)
    async with serve(fake.app) as base:
        before = client_tasks()
        with pytest.raises(McpFailure) as err:
            await client.call_tool(Endpoint(f"{base}/mcp"), "x", {}, deadline_s=0.5)
        await asyncio.sleep(0.2)
        leaked = client_tasks() - before
    assert err.value.code == "timeout"
    assert not leaked, [t.get_coro().__qualname__ for t in leaked]
