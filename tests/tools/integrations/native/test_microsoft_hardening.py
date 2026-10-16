"""Final fix wave: download hardening, redirect refusal, Teams 403 granularity, mailbox-less accounts."""

import gzip

import httpx
import pytest

from mavis.domain.errors import FailureKind as K
from mavis.tools.integrations.native.graph import GraphError
from mavis.tools.integrations.native.graph_files import trusted_download_url

from .graphfake import FakeGraph, graph_error, graph_item
from .test_microsoft_transport import U

GRAPH = "https://graph.microsoft.com/v1.0"
CHAT_MESSAGES = f"{GRAPH}/me/chats/19:abc@unq.gbl.spaces/messages"


# --- A2: download hardening ---
@pytest.mark.parametrize("host", ["a_b.sharepoint.com", "a$b.sharepoint.com", "a..b.sharepoint.com",
                                  "-a.sharepoint.com", "a b.sharepoint.com", "a!.1drv.com",
                                  "a'.sharepoint.com", ".sharepoint.com"])
def test_download_host_characters_are_allowlisted(host):
    assert trusted_download_url(f"https://{host}/dl") is None


@pytest.mark.parametrize("host", ["corp-my.sharepoint.com", "a.b.c.sharepoint.com", "xn--caf-dma.1drv.com",
                                  "CORP.SharePoint.com", "corp.sharepoint.com."])
def test_ordinary_download_hosts_pass(host):
    assert trusted_download_url(f"https://{host}/dl") is not None


def _wire(content: bytes, encoding: str) -> httpx.Response:
    """A response whose body stays encoded on the wire (httpx only decodes it when read)."""
    return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=httpx.ByteStream(content))


def _read_setup(download, *, mime="text/plain"):
    g = FakeGraph()
    g.on("GET", r"/v1.0/me/drive/items/01T$", httpx.Response(200, json=graph_item("01T", size=20, mime=mime)))
    g.on("GET", r"/v1.0/me/drive/items/01T/content$",
         httpx.Response(302, headers={"Location": "https://x.sharepoint.com/dl?sig=SECRET"}))
    g.on("GET", r"/dl$", download)
    return g


async def test_download_asks_for_identity_encoding_and_sends_no_credentials():
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, content=b"hello")

    g = _read_setup(answer)
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.ok and r.data["text"] == "hello"
    req = seen[0]
    assert req.headers["accept-encoding"] == "identity"
    assert "authorization" not in req.headers and "prefer" not in req.headers


async def test_download_cookies_are_isolated_from_the_graph_client():
    seen = []

    def answer(request):
        seen.append(request)
        cookie = "FedAuth=SECRET; Domain=sharepoint.com"
        return httpx.Response(200, content=b"hello", headers={"Set-Cookie": cookie})

    g = _read_setup(answer)
    ex, _ = g.executor()
    ex.graph.http.cookies.set("session", "graph-cookie", domain="x.sharepoint.com")
    ex.graph.http.cookies.set("other", "graph-cookie2", domain="graph.microsoft.com")
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.ok
    assert "cookie" not in seen[0].headers  # the Graph client's jar is not sent to the download host
    assert "FedAuth" not in {c.name for c in ex.graph.http.cookies.jar}  # and nothing is stored in it
    later = [q for q in g.requests if q.url.path.endswith("/content")][0]
    assert "FedAuth" not in later.headers.get("cookie", "")


async def test_download_cap_counts_decoded_bytes_not_wire_bytes():
    bomb = gzip.compress(b"a" * 5_000_000)
    assert len(bomb) < 50_000
    g = _read_setup(_wire(bomb, "gzip"))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.ok and r.data["truncated"] is True and len(r.data["text"]) == 1_000_000


async def test_small_gzip_download_is_decoded():
    g = _read_setup(_wire(gzip.compress(b"hello gz"), "gzip"))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.ok and r.data["text"] == "hello gz" and r.data["truncated"] is False


async def test_unknown_or_broken_content_encoding_is_a_failed_download():
    for headers, content in (({"Content-Encoding": "br"}, b"xx"),
                             ({"Content-Encoding": "gzip"}, b"not gzip")):
        g = _read_setup(_wire(content, headers["Content-Encoding"]))
        ex, _ = g.executor()
        r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
        assert not r.ok and r.error_kind is K.UNAVAILABLE and "SECRET" not in r.error


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_an_error_for_raw_and_json(status):
    g = FakeGraph().on("GET", r"/v1.0/me/drive/items/01T$",
                       httpx.Response(status, headers={"Location": "https://evil.test/x"}))
    ex, _ = g.executor()
    with pytest.raises(GraphError):
        await ex.graph.raw(1, "GET", f"{GRAPH}/me/drive/items/01T")
    with pytest.raises(GraphError):
        await ex.graph.json(1, "GET", f"{GRAPH}/me/drive/items/01T")
    assert all(q.url.host == "graph.microsoft.com" for q in g.requests)


# --- A3: Teams 403 granularity ---
async def _teams_403(code, message):
    g = FakeGraph().on("GET", r"/messages$", graph_error(403, code, message))
    ex, _ = g.executor()
    with pytest.raises(GraphError) as e:
        await ex.graph.json(1, "GET", CHAT_MESSAGES, on_forbidden=K.PERMISSION_MISSING)
    return e.value


@pytest.mark.parametrize("code,message", [
    ("Forbidden", "Missing scope permissions on the request. API requires one of 'Chat.Read'."),
    ("Forbidden", "Missing role permissions on the request."),
    ("Authorization_RequestDenied", "Insufficient privileges to complete the operation."),
    ("Forbidden", "AADSTS65001: admin consent is required for tenant corp-secret."),
])
async def test_teams_403_for_missing_permission_or_admin_consent(code, message):
    err = await _teams_403(code, message)
    assert err.kind is K.PERMISSION_MISSING
    assert "permission" in err.detail and "admin consent" in err.detail
    assert "Chat.Read" not in err.detail and "corp-secret" not in err.detail


@pytest.mark.parametrize("message", [
    "User is not a member of the chat.", "Caller is not a member of this chat 19:abc@unq.gbl.spaces",
    "The user is not a participant in the chat.",
])
async def test_teams_403_for_a_chat_the_user_is_not_in(message):
    err = await _teams_403("Forbidden", message)
    assert err.kind is K.NOT_FOUND
    assert "not a member of that chat" in err.detail and "19:abc" not in err.detail


async def test_teams_403_with_no_recognised_reason_stays_a_missing_permission():
    err = await _teams_403("Forbidden", "mystery tenant-secret")
    assert err.kind is K.PERMISSION_MISSING and "secret" not in err.detail


async def test_403_hint_never_applies_to_drive_endpoints():
    g = FakeGraph().on(
        "GET", r"/items/", graph_error(403, "accessDenied", "User is not a member of the chat."))
    ex, _ = g.executor()
    with pytest.raises(GraphError) as e:
        await ex.graph.json(1, "GET", f"{GRAPH}/me/drive/items/01A", on_forbidden=K.PERMISSION_MISSING)
    assert e.value.kind is K.PERMISSION_MISSING and e.value.detail == "microsoft 403 accessDenied"


# --- A4: an account without an Outlook mailbox ---
@pytest.mark.parametrize("status,code,url", [
    (404, "MailboxNotEnabledForRESTAPI", f"{GRAPH}/me/messages"),
    (404, "MailboxNotEnabledForRESTAPI", f"{GRAPH}/me/mailFolders/inbox/messages/delta"),
    (404, "MailboxNotSupportedForRESTAPI", f"{GRAPH}/me/mailFolders/inbox"),
    (403, "ErrorAccessDenied", f"{GRAPH}/me/sendMail"),
])
async def test_no_mailbox_is_not_reported_as_not_found(status, code, url):
    method = "POST" if url.endswith("sendMail") else "GET"
    g = FakeGraph().on(method, r"/me/", graph_error(status, code, "mail@corp"))
    ex, _ = g.executor()
    with pytest.raises(GraphError) as e:
        if url.endswith("sendMail"):
            await ex.graph.json(1, "POST", url, body={})
        else:
            await ex.graph.json(1, "GET", url)
    assert e.value.kind is K.UNAVAILABLE
    assert "no Outlook mailbox" in e.value.detail and "mail@corp" not in e.value.detail


async def test_item_level_mail_errors_are_still_not_found():
    for status, code in ((404, "ErrorItemNotFound"), (403, "ErrorAccessDenied")):
        g = FakeGraph().on("GET", r"/me/messages/", graph_error(status, code))
        ex, _ = g.executor()
        with pytest.raises(GraphError) as e:
            await ex.graph.json(1, "GET", f"{GRAPH}/me/messages/AAA")
        assert e.value.kind is K.NOT_FOUND


# --- fix round 2: a keyed write that was retried and still fails is UNCONFIRMED ---
async def _keyed_post(responses):
    g = FakeGraph().on("POST", r"/me/events$", responses)
    ex, sleeps = g.executor()
    with pytest.raises(GraphError) as e:
        await ex.graph.json(1, "POST", f"{GRAPH}/me/events", body={"transactionId": "t"}, idempotent=True)
    return e.value, len(g.requests)


async def test_keyed_write_503_then_503_is_unconfirmed():
    err, calls = await _keyed_post([graph_error(503, "serviceNotAvailable")])
    assert err.kind is K.UNCONFIRMED and calls == 2 and "check" in err.detail


async def test_keyed_write_transport_error_twice_is_unconfirmed():
    def boom(request):
        raise httpx.ReadError("x", request=request)

    g = FakeGraph().on("POST", r"/me/events$", boom)
    ex, _ = g.executor()
    with pytest.raises(GraphError) as e:
        await ex.graph.json(1, "POST", f"{GRAPH}/me/events", body={}, idempotent=True)
    assert e.value.kind is K.UNCONFIRMED and len(g.requests) == 2


async def test_keyed_write_504_with_long_retry_after_is_unconfirmed():
    err, calls = await _keyed_post([graph_error(504, "gatewayTimeout", headers={"Retry-After": "600"})])
    assert err.kind is K.UNCONFIRMED and calls == 1


async def test_keyed_write_504_retry_after_twice_is_unconfirmed():
    err, calls = await _keyed_post([graph_error(504, "gatewayTimeout", headers={"Retry-After": "1"})])
    assert err.kind is K.UNCONFIRMED and calls == 2


async def test_keyed_write_5xx_then_429_is_unconfirmed():
    err, _ = await _keyed_post([graph_error(500, "x"), graph_error(429, "x", headers={"Retry-After": "99"})])
    assert err.kind is K.UNCONFIRMED


async def test_reads_and_unsent_throttles_keep_their_kinds():
    g = FakeGraph().on("GET", r"/me/events$", graph_error(503, "x"))
    ex, _ = g.executor()
    with pytest.raises(GraphError) as e:
        await ex.graph.json(1, "GET", f"{GRAPH}/me/events")
    assert e.value.kind is K.UNAVAILABLE
    g = FakeGraph().on("POST", r"/me/events$", graph_error(429, "x", headers={"Retry-After": "99"}))
    ex, _ = g.executor()
    with pytest.raises(GraphError) as e:
        await ex.graph.json(1, "POST", f"{GRAPH}/me/events", body={}, idempotent=True)
    assert e.value.kind is K.RATE_LIMITED
