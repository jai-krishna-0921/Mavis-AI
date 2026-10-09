import httpx
import pytest

from mavis.tools.integrations.native.http import ResponseTooLarge, make_client, retry_after_s, send_capped


async def test_reads_a_small_body():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"a": 1}))
    async with httpx.AsyncClient(transport=transport) as c:
        r = await send_capped(c, "GET", "https://x.test/", max_bytes=100)
    assert r.status_code == 200 and r.json() == {"a": 1}


@pytest.mark.parametrize("size,cap,ok", [(100, 100, True), (101, 100, False), (10_000, 100, False)])
async def test_cap_applies_to_streamed_bodies(size, cap, ok):
    def handler(request):
        async def body():
            for _ in range(size // 50):
                yield b"x" * 50
            if size % 50:
                yield b"x" * (size % 50)
        return httpx.Response(200, content=body())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        if ok:
            assert len((await send_capped(c, "GET", "https://x.test/", max_bytes=cap)).content) == size
        else:
            with pytest.raises(ResponseTooLarge):
                await send_capped(c, "GET", "https://x.test/", max_bytes=cap)


async def test_declared_length_over_cap_is_refused_early():
    handler = lambda r: httpx.Response(200, headers={"content-length": "999999"}, content=b"x")  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ResponseTooLarge):
            await send_capped(c, "GET", "https://x.test/", max_bytes=1000)


async def test_gzip_body_is_counted_after_decoding():
    import gzip
    raw = gzip.compress(b"y" * 5000)
    handler = lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, content=raw)  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ResponseTooLarge):
            await send_capped(c, "GET", "https://x.test/", max_bytes=1000)
        assert (await send_capped(c, "GET", "https://x.test/", max_bytes=10_000)).content == b"y" * 5000


async def test_client_does_not_follow_redirects_and_has_timeouts():
    c = make_client(transport=httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"location": "https://evil.test/"})))
    async with c:
        r = await c.get("https://x.test/")
    assert r.status_code == 302 and c.timeout.read is not None and not c.follow_redirects


@pytest.mark.parametrize("header,expected",
                         [("7", 7.0), ("0", 0.0), ("junk", 30.0), (None, 30.0), ("-5", 0.0)])
def test_retry_after(header, expected):
    r = httpx.Response(429, headers={"retry-after": header} if header is not None else {})
    assert retry_after_s(r) == expected
