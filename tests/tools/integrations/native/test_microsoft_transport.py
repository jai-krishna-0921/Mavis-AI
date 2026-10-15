import httpx
import pytest

from mavis.domain.errors import FailureKind as K
from mavis.domain.integrations import UserRef
from mavis.tools.integrations.native.graph import GraphError, seg, strip_ms, with_ms

from .graphfake import FakeGraph, GraphTokens, graph_error, page

U = UserRef(user_id=1)
PROFILE = r"/v1.0/me$"
SEND = "https://graph.microsoft.com/v1.0/me/sendMail"


def ok_me():
    return httpx.Response(200, json={"mail": "me@orbit.test", "userPrincipalName": "me@orbit.test"})


async def test_401_refreshes_once_then_succeeds():
    g, t = FakeGraph(), GraphTokens()
    g.on("GET", PROFILE, [graph_error(401, "InvalidAuthenticationToken"), ok_me()])
    ex, _ = g.executor(t)
    r = await ex.execute(U, "mail.profile", {})
    assert r.ok and t.calls == [False, True]


async def test_second_401_is_an_auth_failure_and_carries_no_vendor_text():
    g = FakeGraph().on(
        "GET", PROFILE, graph_error(401, "InvalidAuthenticationToken", "tenant secret-corp-name"))
    ex, _ = g.executor()
    r = await ex.execute(U, "mail.profile", {})
    assert not r.ok and r.error_kind is K.AUTH and "secret-corp-name" not in r.error


async def test_revoked_grant_maps_to_auth():
    ex, _ = FakeGraph().executor(GraphTokens(revoked=True))
    assert (await ex.execute(U, "mail.profile", {})).error_kind is K.AUTH


async def test_429_waits_retry_after_once():
    g = FakeGraph().on(
        "GET", PROFILE, [graph_error(429, "TooManyRequests", headers={"Retry-After": "3"}), ok_me()])
    ex, sleeps = g.executor()
    assert (await ex.execute(U, "mail.profile", {})).ok and sleeps == [3.0]


async def test_long_retry_after_is_rate_limited_without_waiting():
    g = FakeGraph().on("GET", PROFILE, graph_error(429, "TooManyRequests", headers={"Retry-After": "120"}))
    ex, sleeps = g.executor()
    r = await ex.execute(U, "mail.profile", {})
    assert r.error_kind is K.RATE_LIMITED and sleeps == []


async def test_503_with_retry_after_repeats_even_a_write_but_504_does_not():
    g = FakeGraph().on("POST", r"/v1.0/me/sendMail$", [
        graph_error(503, "serviceNotAvailable", headers={"Retry-After": "1"}), httpx.Response(202)])
    ex, sleeps = g.executor()
    await ex.graph.json(1, "POST", SEND, body={})
    assert sleeps == [1.0]
    g2 = FakeGraph().on("POST", r"/v1.0/me/sendMail$", graph_error(504, "gatewayTimeout"))
    ex2, _ = g2.executor()
    with pytest.raises(GraphError) as e:
        await ex2.graph.json(1, "POST", SEND, body={})
    assert e.value.kind is K.UNCONFIRMED and "may or may not" in e.value.detail
    assert len(g2.requests) == 1


async def test_5xx_on_a_get_repeats_once_then_unavailable():
    g = FakeGraph().on("GET", PROFILE, graph_error(502, "badGateway"))
    ex, sleeps = g.executor()
    r = await ex.execute(U, "mail.profile", {})
    assert r.error_kind is K.UNAVAILABLE and len(g.requests) == 2 and sleeps == [1.0]


async def test_read_error_on_a_write_is_unconfirmed_and_connect_error_repeats():
    calls = {"n": 0}

    def boom(request):
        calls["n"] += 1
        raise httpx.ReadError("x")
    g = FakeGraph().on("POST", r"/v1.0/me/sendMail$", boom)
    ex, _ = g.executor()
    with pytest.raises(GraphError) as e:
        await ex.graph.json(1, "POST", SEND, body={})
    assert e.value.kind is K.UNCONFIRMED and calls["n"] == 1

    attempts = {"n": 0}

    def flaky(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("x")
        return httpx.Response(202)
    g2 = FakeGraph().on("POST", r"/v1.0/me/sendMail$", flaky)
    ex2, _ = g2.executor()
    await ex2.graph.json(1, "POST", SEND, body={})
    assert attempts["n"] == 2


async def test_oversized_response_is_cut_and_reported():
    g = FakeGraph().on("GET", PROFILE, httpx.Response(200, content=b"x" * 5000))
    ex, _ = g.executor()
    ex.graph.max_json = 1000
    r = await ex.execute(U, "mail.profile", {})
    assert not r.ok and "size limit" in r.error


@pytest.mark.parametrize("status,code,kind", [
    (400, "BadRequest", K.INVALID_ARGUMENT), (404, "ErrorItemNotFound", K.NOT_FOUND),
    (400, "ErrorInvalidIdMalformed", K.NOT_FOUND), (403, "ErrorAccessDenied", K.NOT_FOUND),
    (409, "ErrorInvalidChangeKey", K.INVALID_ARGUMENT), (410, "syncStateNotFound", K.NOT_FOUND),
    (418, "teapot", K.UNKNOWN),
])
def test_classification(status, code, kind):
    from mavis.tools.integrations.native.graph import classify
    e = classify(status, code, f"microsoft {status} {code}")
    assert e.kind is kind and "secret" not in e.detail


def test_403_on_chats_and_files_is_a_missing_permission():
    from mavis.tools.integrations.native.graph import classify
    url = "https://graph.microsoft.com/v1.0/me/chats"
    assert classify(403, "Forbidden", "microsoft 403 Forbidden", url).kind is K.PERMISSION_MISSING


@pytest.mark.parametrize(
    "bad", ["", ".", "..", "a/b", "a\\b", "a b ", "x\x00y", "a?b", "a#b", "a%2Fb", "a%2e%2e"])
def test_hostile_ids_are_refused(bad):
    with pytest.raises(GraphError) as e:
        seg(bad)
    assert e.value.kind is K.INVALID_ARGUMENT


@pytest.mark.parametrize("good", [
    "AAMkAGI2TG93AAA=", "AQMkADAwATM3ZmYAZS0wZTI5LWQ4NWYtMDACLTAwCgBGAAAD", "19:abc_def@unq.gbl.spaces",
    "19:meeting_MjZl@thread.v2", "01ABCDEFGH2345", "ms:AAMk-_=="])
def test_real_graph_ids_pass(good):
    assert seg(good)
    assert not seg(good).startswith("ms%3A") and "/" not in seg(good)


def test_ms_prefix_helpers():
    assert strip_ms("ms:abc") == "abc" and strip_ms("abc") == "abc" and with_ms("abc") == "ms:abc"
    assert with_ms("ms:abc") == "ms:abc"


async def test_invalid_id_arg_returns_invalid_argument_before_any_request():
    g = FakeGraph()
    ex, _ = g.executor()
    ex._handlers["mail.read"] = lambda uid, a: None   # wired for real in Task 4; the id gate runs first
    r = await ex.execute(U, "mail.read", {"message_id": "../me"})
    assert r.error_kind is K.INVALID_ARGUMENT and r.error_field == "message_id" and g.requests == []


async def test_pages_follow_next_link_and_expose_delta_link():
    g = FakeGraph()
    base = "https://graph.microsoft.com/v1.0/me/messages"
    first = page([{"id": "1"}], next_link=base + "?$skiptoken=a")
    second = page([{"id": "2"}], delta_link=base + "?$deltatoken=z")
    g.on("GET", r"/v1.0/me/messages$", [httpx.Response(200, json=first), httpx.Response(200, json=second)])
    ex, _ = g.executor()
    items, more, delta = await ex.graph.pages(1, base, {"$top": 1}, 10)
    assert [i["id"] for i in items] == ["1", "2"] and more is None and delta.endswith("deltatoken=z")


async def test_next_link_to_another_host_is_not_followed():
    g = FakeGraph().on("GET", r"/v1.0/me/messages$", httpx.Response(
        200, json=page([{"id": "1"}], next_link="https://evil.test/steal")))
    ex, _ = g.executor()
    with pytest.raises(GraphError):
        await ex.graph.pages(1, "https://graph.microsoft.com/v1.0/me/messages", {}, 10)
    assert all(r.url.host == "graph.microsoft.com" for r in g.requests)
