import httpcore
import httpx
import pytest
import respx

from mavis.tools import web


@pytest.fixture
def tavily_key(settings, monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test")


@pytest.fixture
def no_tavily(settings, monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")


@pytest.fixture
def public_dns(monkeypatch):
    """Skip real DNS: every host is public."""

    async def _public(url: str) -> None:
        return None

    monkeypatch.setattr(web, "assert_public_url", _public)


@respx.mock
async def test_search_uses_tavily(tavily_key):
    route = respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(200, json={
        "answer": "Teamcenter is Siemens PLM software.",
        "results": [{"title": "Teamcenter", "url": "https://siemens.com/tc", "content": "PLM suite"}],
    }))
    out = await web.web_search(1, web.SearchArgs(query="what is teamcenter"))
    assert route.called
    assert route.calls[0].request.headers["Authorization"] == "Bearer tvly-test"
    assert "[1] Summary" in out
    assert "[2] Teamcenter: https://siemens.com/tc" in out
    assert "—" not in out and "–" not in out


@respx.mock
async def test_search_falls_back_to_ddg(tavily_key, monkeypatch):
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(500))

    async def _ddg(query: str, max_results: int) -> list[dict]:
        return [{"title": "DDG hit", "url": "https://example.com", "snippet": "fallback"}]

    monkeypatch.setattr(web, "ddg_search", _ddg)
    out = await web.web_search(1, web.SearchArgs(query="anything"))
    assert "DDG hit" in out


async def test_search_without_key_uses_ddg(no_tavily, monkeypatch):
    async def _ddg(query: str, max_results: int) -> list[dict]:
        return []

    monkeypatch.setattr(web, "ddg_search", _ddg)
    assert await web.web_search(1, web.SearchArgs(query="anything")) == "No results."


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data",
    "http://127.0.0.1:8000/admin",
    "http://10.0.0.5/",
    "http://localhost/",
    "file:///etc/passwd",
    "http://[::ffff:169.254.169.254]/",
    "http://[fd00:ec2::254]/",
    "http://[::1]/",
    "http://[64:ff9b::a9fe:a9fe]/",
    "http://100.100.100.200/",
    "ftp://example.com/",
    "http://user:pw@example.com/",
])
async def test_extract_refuses_private_addresses(url):
    with pytest.raises(ValueError):
        await web.assert_public_url(url)


async def test_public_ip_literal_allowed():
    await web.assert_public_url("http://93.184.216.34/")


async def test_any_private_answer_blocks(monkeypatch):
    def _gai(host, port, *a):
        return [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("10.0.0.1", port))]

    monkeypatch.setattr(web.socket, "getaddrinfo", _gai)
    with pytest.raises(ValueError):
        await web.assert_public_url("https://rebind.example.com/")


async def test_pinned_backend_validates_at_connect_time(monkeypatch):
    """DNS that flips to a private address after any pre-check is refused at connect."""
    monkeypatch.setattr(
        web.socket, "getaddrinfo", lambda host, port, *a: [(2, 1, 6, "", ("169.254.169.254", port))]
    )
    with pytest.raises(ValueError):
        await web._PinnedBackend().connect_tcp("evil.example.com", 80)


async def test_pinned_backend_connects_to_validated_ip(monkeypatch):
    monkeypatch.setattr(
        web.socket, "getaddrinfo", lambda host, port, *a: [(2, 1, 6, "", ("93.184.216.34", port))]
    )
    seen = []

    async def _connect(self, host, port, **kw):
        seen.append(host)
        raise httpcore.ConnectError("stop")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", _connect)
    with pytest.raises(httpcore.ConnectError):
        await web._PinnedBackend().connect_tcp("example.com", 443)
    assert seen == ["93.184.216.34"]


@respx.mock
async def test_extract_falls_back_to_direct_fetch(no_tavily, public_dns):
    respx.get("https://example.com/post").mock(return_value=httpx.Response(
        200, text="<html><script>x()</script><h1>Title</h1><p>Body &amp; more</p></html>"))
    out = await web.web_extract(1, web.ExtractArgs(url="https://example.com/post"))
    assert out == "Title Body & more"


@respx.mock
async def test_extract_uses_tavily_first(tavily_key, public_dns):
    respx.post("https://api.tavily.com/extract").mock(return_value=httpx.Response(
        200, json={"results": [{"raw_content": "from tavily"}]}))
    assert await web.extract("https://example.com/a") == "from tavily"


@respx.mock
async def test_redirect_to_private_host_is_refused(no_tavily, monkeypatch):
    async def _guard(url: str) -> None:
        if "169.254" in url:
            raise ValueError("refusing to fetch non-public address 169.254.169.254")

    monkeypatch.setattr(web, "assert_public_url", _guard)
    respx.get("https://example.com/r").mock(return_value=httpx.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data"}))
    meta = respx.get("http://169.254.169.254/latest/meta-data").mock(
        return_value=httpx.Response(200, text="secret"))
    with pytest.raises(ValueError):
        await web.extract("https://example.com/r")
    assert not meta.called


@respx.mock
async def test_public_redirect_is_followed_and_loops_capped(no_tavily, public_dns):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(302, headers={"location": "/b"}))
    respx.get("https://example.com/b").mock(return_value=httpx.Response(200, text="<p>ok</p>"))
    assert await web.extract("https://example.com/a") == "ok"
    respx.get("https://example.com/loop").mock(
        return_value=httpx.Response(302, headers={"location": "/loop"}))
    with pytest.raises(ValueError, match="too many redirects"):
        await web.extract("https://example.com/loop")


@respx.mock
async def test_response_size_is_capped(no_tavily, public_dns, monkeypatch):
    monkeypatch.setattr(web, "_MAX_BODY_BYTES", 1000)
    respx.get("https://example.com/big").mock(return_value=httpx.Response(200, text="a" * 50_000))
    out = await web.extract("https://example.com/big", max_chars=50_000)
    assert len(out) == 1000


def test_tools_are_read_only_and_untrusted():
    by_name = {t.name: t for t in web.TOOLS}
    assert set(by_name) == {"web_search", "web_extract"}
    assert all(t.untrusted_output for t in web.TOOLS)
    assert all(t.risk.value == "read" for t in web.TOOLS)
    assert "conversation" in by_name["web_search"].agents


def test_pinned_client_uses_pinned_backend_via_public_api():
    client = web._pinned_client()
    pool = client._transport._pool
    assert isinstance(pool, httpcore.AsyncConnectionPool)
    assert isinstance(pool._network_backend, web._PinnedBackend)


async def test_pinned_transport_refuses_private_host(monkeypatch):
    monkeypatch.setattr(web.socket, "getaddrinfo", lambda h, p, *a: [(2, 1, 6, "", ("127.0.0.1", p))])
    async with web._pinned_client() as client:
        with pytest.raises(ValueError):
            await client.get("http://rebind.example.com/")


async def test_ddg_no_results_exception_returns_empty(monkeypatch):
    import ddgs
    from ddgs.exceptions import DDGSException

    class _D:
        def text(self, *a, **k):
            raise DDGSException("No results found.")

    monkeypatch.setattr(ddgs, "DDGS", _D)
    assert await web.ddg_search("zzz", 3) == []


@respx.mock
async def test_tavily_answer_truncated(tavily_key):
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json={"answer": "x" * 900, "results": []}))
    rows = await web.tavily_search("q", 3)
    assert len(rows[0]["snippet"]) == 500


@pytest.mark.parametrize("url,is_search", [
    ("https://www.amazon.in/s?k=electric+standing+desk", True),
    ("https://www.flipkart.com/search?q=standing%20desk", True),
    ("https://www.ikea.com/in/en/search/?q=desk", True),
    ("https://shop.example.com/results/desks", True),
    ("https://www.amazon.in/Flexispot-E7-Standing-Desk/dp/B08XYZ1234", False),
    ("https://www.flipkart.com/flexispot-e7/p/itm123?pid=DSKXYZ", False),
    ("https://siemens.com/tc", False),
    ("https://example.com/blog/best-search-engines", False),
])
def test_search_and_listing_pages_are_told_apart_from_product_pages(url, is_search):
    assert web.looks_like_search_page(url) is is_search


@respx.mock
async def test_search_marks_result_urls_that_are_only_search_pages(tavily_key):
    """Live E2E 2026-10-08: "links" to Amazon search pages were presented as links to the exact model."""
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(200, json={"results": [
        {"title": "Standing desk", "url": "https://www.amazon.in/s?k=standing+desk", "content": "results"},
        {"title": "E7 Pro", "url": "https://www.amazon.in/Flexispot-E7/dp/B08XYZ1234",
         "content": "Rs 14,999"},
    ]}))
    out = await web.web_search(1, web.SearchArgs(query="electric standing desk under 15k"))
    assert "https://www.amazon.in/s?k=standing+desk" + web.SEARCH_PAGE_NOTE in out
    assert "dp/B08XYZ1234\n" in out and "dp/B08XYZ1234" + web.SEARCH_PAGE_NOTE not in out


def test_the_web_rule_grounds_links_in_results():
    from mavis.agents.conversation import WEB_RULE

    assert "only URLs that web_search or web_extract returned" in WEB_RULE
    assert "Never build a link" in WEB_RULE and "search or listing page" in WEB_RULE
