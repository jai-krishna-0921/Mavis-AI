"""The outbound-URL guard: https only, public addresses only, pinned connections, no redirects, caps."""

from __future__ import annotations

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.responses import RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from mavis.connectors import netguard
from mavis.connectors.netguard import (
    ResponseTooLarge,
    SdkResponseTooLarge,
    UnsafeUrl,
    blocked_ip,
    guarded_client,
    guarded_mcp_client,
    same_site,
)
from tests.mcp.localserver import serve


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
    "224.0.0.1", "240.0.0.1", "::1", "::", "fe80::1", "fc00::1", "fd00:ec2::254", "ff02::1",
    "::ffff:127.0.0.1", "64:ff9b::a9fe:a9fe",
    "::ffff:169.254.169.254", "2002:7f00:0001::", "not-an-ip", "192.0.2.1", "198.18.0.1",
])
def test_non_public_addresses_are_blocked(ip):
    assert blocked_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_pass(ip):
    assert not blocked_ip(ip)


@pytest.mark.parametrize("url", [
    "http://example.com/x", "ftp://example.com", "https://user:pw@example.com/", "https://127.0.0.1/",
    "https://[::1]/", "https://169.254.169.254/latest", "https://localhost/", "https://metadata.google.internal/",
    "https://printer.local/", "https://svc.internal/", "//example.com", "example.com", "", "https:///x",
    "https://example.com:notaport/", "http://localhost:8000/mcp", "http://127.0.0.1:9/mcp",
])
async def test_bad_urls_are_refused_before_any_network(settings, url):
    with pytest.raises(UnsafeUrl):
        await netguard.check_url(url)


@pytest.mark.parametrize("answer", [
    ["10.0.0.1"], ["93.184.216.34", "127.0.0.1"], [], ["::1"], ["169.254.169.254"]])
async def test_a_host_with_any_private_address_is_refused(monkeypatch, answer):
    async def resolver(host, port):
        return answer

    monkeypatch.setattr(netguard, "_resolver", resolver)
    with pytest.raises(UnsafeUrl):
        await netguard.check_url("https://rebind.example.com/x")


async def test_an_unresolvable_host_is_refused(monkeypatch):
    async def resolver(host, port):
        raise OSError("nxdomain")

    monkeypatch.setattr(netguard, "_resolver", resolver)
    with pytest.raises(UnsafeUrl):
        await netguard.check_url("https://nowhere.example.com/")


@pytest.mark.parametrize("backend_cls", ["GuardedBackend", "McpBackend"])
async def test_dns_rebinding_the_connection_uses_the_validated_address(monkeypatch, backend_cls):
    """First lookup public, second private: the backend resolves, validates and connects to that address."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    connected = []

    async def resolver(host, port):
        return next(answers)

    class Inner:
        async def connect_tcp(self, host, port, **kw):
            connected.append(host)
            return object()

    monkeypatch.setattr(netguard, "_resolver", resolver)
    backend = getattr(netguard, backend_cls)()
    backend._inner = Inner()
    await backend.connect_tcp("rebind.example.com", 443)
    assert connected == ["93.184.216.34"]
    with pytest.raises(UnsafeUrl):  # the next resolution is private: refused, nothing connected
        await backend.connect_tcp("rebind.example.com", 443)
    assert connected == ["93.184.216.34"]


@pytest.mark.parametrize("backend_cls", ["GuardedBackend", "McpBackend"])
async def test_unix_sockets_are_refused(backend_cls):
    with pytest.raises(UnsafeUrl):
        await getattr(netguard, backend_cls)().connect_unix_socket("/var/run/docker.sock")


@pytest.mark.parametrize(("host", "base", "allowed", "ok"), [
    ("mcp.shop.example.com", "mcp.shop.example.com", (), True),
    ("auth.shop.example.com", "mcp.shop.example.com", (), True),  # siblings under shop.example.com
    ("login.shop.example.com", "shop.example.com", (), True),  # subdomain
    ("shop.example.com", "mcp.shop.example.com", (), True),  # parent
    ("evil.example.com", "mcp.shop.example.com", (), False),  # shares only example.com
    ("evil.com", "shop.com", (), False),
    ("a.co.uk", "b.co.uk", (), False),
    ("evil.test", "mcp.shop.example.com", (), False),
    ("auth.vendor.net", "mcp.shop.example.com", ("auth.vendor.net",), True),
    ("AUTH.VENDOR.NET.", "mcp.shop.example.com", ("auth.vendor.net",), True),  # case and root dot ignored
    ("evil.auth.vendor.net", "mcp.shop.example.com", ("auth.vendor.net",), False),  # allowlist is exact
    ("notshop.example.com.evil.com", "shop.example.com", (), False),
    ("xshop.example.com", "shop.example.com", (), False),
])
def test_same_site(host, base, allowed, ok):
    assert same_site(host, base, allowed) is ok


# --- the localhost allowance (dev and tests only) -------------------------------------------------

async def test_localhost_is_accepted_only_with_the_setting(localhost_ok):
    for url in ("http://localhost:8000/mcp", "http://127.0.0.1:9/mcp", "https://localhost/x"):
        assert (await netguard.check_url(url)).hostname in {"localhost", "127.0.0.1"}


async def test_the_setting_never_opens_other_private_hosts(localhost_ok):
    for url in ("http://10.0.0.5/x", "http://192.168.1.1/x", "http://printer.local/x", "https://169.254.169.254/"):
        with pytest.raises(UnsafeUrl):
            await netguard.check_url(url)


async def test_the_setting_is_ignored_in_prod(settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("MCP_ALLOW_LOCALHOST", "true")
    get_settings.cache_clear()
    with pytest.raises(UnsafeUrl):
        await netguard.check_url("http://localhost:8000/mcp")


# --- the guarded client (httpx, mocked) -----------------------------------------------------------

@respx.mock
async def test_redirects_are_errors_never_followed(settings):
    hop = respx.get("https://a.example.com/x").mock(return_value=httpx.Response(
        302, headers={"location": "https://169.254.169.254/latest/meta-data"}))
    target = respx.get("https://169.254.169.254/latest/meta-data")
    async with guarded_client() as c:
        with pytest.raises(UnsafeUrl, match="redirect"):
            await c.get("https://a.example.com/x")
    assert hop.called and not target.called


@respx.mock
async def test_the_client_refuses_private_hosts_before_sending(monkeypatch):
    async def resolver(host, port):
        return ["10.1.1.1"]

    monkeypatch.setattr(netguard, "_resolver", resolver)
    route = respx.get("https://internal-lookalike.example.com/x")
    async with guarded_client() as c:
        with pytest.raises(UnsafeUrl):
            await c.get("https://internal-lookalike.example.com/x")
    assert not route.called


@respx.mock
async def test_a_declared_oversize_body_is_refused_up_front(settings):
    respx.get("https://a.example.com/big").mock(return_value=httpx.Response(
        200, headers={"content-length": "999999999"}, content=b"x"))
    async with guarded_client(max_bytes=1000) as c:
        with pytest.raises(ResponseTooLarge):
            await c.get("https://a.example.com/big")


@respx.mock
async def test_a_streamed_body_is_aborted_at_the_cap_before_it_is_buffered(settings):
    pulled = []

    async def body():
        for i in range(1000):
            pulled.append(i)
            yield b"x" * 100

    respx.get("https://a.example.com/stream").mock(return_value=httpx.Response(200, content=body()))
    async with guarded_client(max_bytes=1000) as c:
        with pytest.raises(ResponseTooLarge):
            await c.get("https://a.example.com/stream")
    assert len(pulled) < 20  # stopped near the cap, not after reading all 100 KB


@respx.mock
async def test_the_per_request_cap_overrides_the_client_cap(settings):
    respx.get("https://a.example.com/s").mock(return_value=httpx.Response(200, content=b"y" * 500))
    async with guarded_client(max_bytes=10_000) as c:
        with pytest.raises(ResponseTooLarge):
            await c.get("https://a.example.com/s", extensions={"max_bytes": 100})
        assert (await c.get("https://a.example.com/s")).content == b"y" * 500


@respx.mock
async def test_a_normal_response_passes_and_the_client_has_bounded_timeouts(settings):
    respx.get("https://a.example.com/ok").mock(return_value=httpx.Response(200, json={"a": 1}))
    async with guarded_client() as c:
        assert (await c.get("https://a.example.com/ok")).json() == {"a": 1}
        t = c.timeout
        assert (t.connect, t.read) == (10.0, 60.0)
        assert all(v is not None and v <= 60 for v in (t.connect, t.read, t.write, t.pool))
        assert c.follow_redirects is False


# --- the guarded MCP client (httpx2, against a real local server) ---------------------------------

def _app():
    async def big(_request):
        return Response(b"z" * 5000, headers={"content-type": "text/plain"})

    async def stream(_request):
        async def gen():
            for _ in range(200):
                yield b"s" * 100
        return StreamingResponse(gen())

    async def hop(_request):
        return RedirectResponse("/ok", status_code=307)

    async def ok(_request):
        return Response(b'{"a": 1}', headers={"content-type": "application/json"})

    routes = [Route("/big", big), Route("/stream", stream), Route("/hop", hop), Route("/ok", ok)]
    return Starlette(routes=routes)


async def test_mcp_client_passes_a_normal_response_with_bounded_timeouts(localhost_ok):
    async with serve(_app()) as base:
        async with guarded_mcp_client(headers={"x-test": "1"}) as c:
            assert (await c.get(f"{base}/ok")).json() == {"a": 1}
            assert (c.timeout.connect, c.timeout.read) == (10.0, 60.0)
            assert c.follow_redirects is False


async def test_mcp_client_refuses_a_declared_oversize_body(localhost_ok):
    async with serve(_app()) as base, guarded_mcp_client(max_bytes=1000) as c:
        with pytest.raises(SdkResponseTooLarge):
            await c.get(f"{base}/big")


async def test_mcp_client_aborts_a_streamed_body_at_the_cap(localhost_ok):
    async with serve(_app()) as base, guarded_mcp_client(max_bytes=1000) as c:
        with pytest.raises(SdkResponseTooLarge):
            await c.get(f"{base}/stream")


async def test_mcp_client_never_follows_a_redirect(localhost_ok):
    async with serve(_app()) as base, guarded_mcp_client() as c:
        with pytest.raises(UnsafeUrl, match="redirect"):
            await c.get(f"{base}/hop")


async def test_mcp_client_refuses_localhost_without_the_setting(settings):
    async with guarded_mcp_client() as c:
        with pytest.raises(UnsafeUrl):
            await c.get("http://127.0.0.1:9/ok")


async def test_mcp_client_refuses_private_hosts_before_sending(monkeypatch, settings):
    async def resolver(host, port):
        return ["10.1.1.1"]

    monkeypatch.setattr(netguard, "_resolver", resolver)
    async with guarded_mcp_client() as c:
        with pytest.raises(UnsafeUrl):
            await c.get("https://internal-lookalike.example.com/x")


async def test_mcp_client_factory_signature_matches_the_sdk(localhost_ok):
    """The SDK's SSE transport calls the factory with headers, timeout and auth as keywords."""
    c = guarded_mcp_client(headers={"a": "b"}, timeout=netguard.MCP_TIMEOUT, auth=None)
    assert c.headers["a"] == "b"
    await c.aclose()


# --- compressed bodies (the cap counts wire bytes, so encoded responses are refused) ---------------

@respx.mock
async def test_a_compressed_response_is_refused_and_identity_is_requested(settings):
    import gzip

    route = respx.get("https://a.example.com/bomb").mock(return_value=httpx.Response(
        200, headers={"content-encoding": "gzip"}, content=gzip.compress(b"0" * 1_000_000)))
    async with guarded_client(max_bytes=1000) as c:
        with pytest.raises(UnsafeUrl, match="compressed"):
            await c.get("https://a.example.com/bomb")
    assert route.calls.last.request.headers["accept-encoding"] == "identity"


@respx.mock
async def test_an_identity_response_under_the_cap_passes(settings):
    respx.get("https://a.example.com/id").mock(return_value=httpx.Response(
        200, headers={"content-encoding": "identity"}, content=b"fine"))
    async with guarded_client(max_bytes=1000) as c:
        assert (await c.get("https://a.example.com/id")).content == b"fine"


def _gzip_app():
    import gzip

    async def bomb(_request):
        return Response(gzip.compress(b"0" * 1_000_000), headers={"content-encoding": "gzip"})

    async def echo(request):
        return Response(request.headers.get("accept-encoding", "").encode())

    return Starlette(routes=[Route("/bomb", bomb), Route("/echo", echo)])


async def test_mcp_client_refuses_a_compressed_response_and_asks_for_identity(localhost_ok):
    async with serve(_gzip_app()) as base, guarded_mcp_client(max_bytes=1000) as c:
        assert (await c.get(f"{base}/echo")).text == "identity"
        with pytest.raises(UnsafeUrl, match="compressed"):
            await c.get(f"{base}/bomb")
