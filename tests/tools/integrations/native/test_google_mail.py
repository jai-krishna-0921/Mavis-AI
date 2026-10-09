"""GoogleExecutor mail actions against a fake Gmail; results go through the real normalizers and renderers."""

from __future__ import annotations

import base64
import email
from email import policy

import httpx
import pytest

from mavis.domain.integrations import UserRef
from mavis.tools.integrations import mail_render
from mavis.tools.integrations.normalize import email_event, extract_messages, normalize_email

from .gfake import FakeGoogle, Tokens, body_of, gmail_message
from .test_gmail_mime import attachment, multi, part

USER = UserRef(user_id=7)
LIST = r"/gmail/v1/users/me/messages$"
GET = r"/gmail/v1/users/me/messages/[^/]+$"


def get_by_id(messages: dict):
    def answer(request: httpx.Request) -> httpx.Response:
        mid = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json=messages[mid]) if mid in messages else httpx.Response(
            404, json={"error": {"code": 404, "message": "Requested entity was not found.",
                                 "errors": [{"reason": "notFound"}]}})
    return answer


async def test_search_paginates_fetches_full_and_parses_through_normalizers():
    msgs = {f"m{i}": gmail_message(f"m{i}", subject=f"Subject {i}", text=f"Body {i}") for i in range(1, 6)}
    msgs["m2"] = gmail_message("m2", labels=("INBOX",), text="Newsletter", extra_headers=(
        ("List-Unsubscribe", "<mailto:unsub@acme.com>"),))
    pages = {None: {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "P2", "resultSizeEstimate": 5},
             "P2": {"messages": [{"id": "m3"}, {"id": "m4"}], "nextPageToken": "P3"},
             "P3": {"messages": [{"id": "m5"}]}}
    fake = FakeGoogle().on("GET", LIST, lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken")]))
    fake.on("GET", GET, get_by_id(msgs))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.search", {"query": "newer_than:7d", "max_results": 4})
    assert res.ok
    raws = extract_messages(res.data)
    assert [m["messageId"] for m in raws] == ["m1", "m2", "m3", "m4"]  # stops at max_results, in order
    lists = fake.calls("GET", LIST)
    assert [r.url.params.get("pageToken") for r in lists] == [None, "P2"]
    assert "-in:spam" in lists[0].url.params["q"] and "-category:promotions" in lists[0].url.params["q"]
    assert all(r.url.params["format"] == "full" for r in fake.calls("GET", GET))
    assert fake.peak <= 5
    events = [email_event(7, r, "poller") for r in raws]
    assert all(events)
    first = events[0].payload
    assert first["from_address"] == "alice@acme.com" and first["subject"] == "Subject 1"
    assert first["sender_authenticated"] is True and first["list_unsubscribe"] is False
    assert first["snippet"] == "Body 1" and "UNREAD" in first["labels"] and first["received_at"]
    assert events[1].payload["list_unsubscribe"] is True
    text = mail_render.render_search(res.data)
    assert "message_id=m1 thread_id=t1 (unread)" in text and "Preview: Body 1" in text


async def test_search_bounds_concurrency_and_skips_messages_deleted_meanwhile():
    ids = [f"m{i}" for i in range(12)]
    msgs = {i: gmail_message(i) for i in ids if i != "m3"}
    fake = FakeGoogle().on("GET", LIST, httpx.Response(200, json={"messages": [{"id": i} for i in ids]}))
    fake.on("GET", GET, get_by_id(msgs))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.search", {"query": "", "max_results": 12})
    assert res.ok and len(res.data["messages"]) == 11
    assert 1 < fake.peak <= 5


async def test_search_with_no_matches_is_ok_and_empty():
    fake = FakeGoogle().on("GET", LIST, httpx.Response(200, json={"resultSizeEstimate": 0}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.search", {"query": "from:nobody", "max_results": 5})
    assert res.ok and res.data["messages"] == []
    assert "No emails matched" in mail_render.render_search(res.data, None)


async def test_read_html_only_message_renders_text_and_lists_attachments():
    html_root = multi("multipart/mixed", part("text/html", "<div>Hi <b>Sam</b></div><script>x()</script>"),
                      attachment("plan.pdf", "application/pdf", 9000))
    fake = FakeGoogle().on("GET", GET, httpx.Response(200, json=gmail_message("m9", payload=html_root)))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.read", {"message_id": "m9"})
    assert res.ok
    assert res.data["messageText"] == "Hi Sam"
    assert res.data["attachments"] == [{"filename": "plan.pdf", "mimeType": "application/pdf", "size": 9000}]
    assert not any("attachments/" in r.url.path for r in fake.requests)  # never downloaded
    out = mail_render.render_read(res.data)
    assert "Subject: Hello" in out and "Hi Sam" in out and "x()" not in out
    assert normalize_email(res.data)["message_id"] == "m9"


async def test_read_caps_body_and_decodes_non_utf8():
    big = gmail_message("m1", payload=part("text/plain", "lorem " * 20_000))
    cp = gmail_message("m2", payload=part("text/plain", "Grüße, señor", charset="iso-8859-1"))
    fake = FakeGoogle().on("GET", GET, get_by_id({"m1": big, "m2": cp}))
    ex, _ = fake.executor()
    r1 = await ex.execute(USER, "mail.read", {"message_id": "m1"})
    assert r1.data["bodyTruncated"] and len(r1.data["messageText"]) <= 20_000
    r2 = await ex.execute(USER, "mail.read", {"message_id": "m2"})
    assert r2.data["messageText"] == "Grüße, señor"


async def test_thread_returns_all_messages_under_messages():
    thread = {"id": "t9", "messages": [gmail_message("a", thread="t9"), gmail_message("b", thread="t9")]}
    fake = FakeGoogle().on("GET", r"/threads/t9$", httpx.Response(200, json=thread))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.thread", {"thread_id": "t9"})
    assert [m["messageId"] for m in extract_messages(res.data)] == ["a", "b"]


async def test_profile_exposes_email_address():
    fake = FakeGoogle().on("GET", r"/profile$", httpx.Response(200, json={"emailAddress": "me@kripya.com"}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.profile", {})
    assert res.data["emailAddress"] == "me@kripya.com"


def decode_raw(request: httpx.Request):
    body = body_of(request)
    raw = body["raw"] if "raw" in body else body["message"]["raw"]
    assert "+" not in raw and "/" not in raw  # base64url alphabet
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


async def test_send_builds_rfc5322_from_the_account():
    fake = FakeGoogle().on("POST", r"/messages/send$", httpx.Response(200, json={"id": "s1", "threadId": "t5", "labelIds": ["SENT"]}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.send", {"to": ["a@x.com", "b@y.com"], "cc": ["c@z.com"],
                                               "subject": "Plan ✓", "body": "Line 1\nLine 2"})
    assert res.ok and res.data["messageId"] == "s1" and res.data["threadId"] == "t5"
    msg = decode_raw(fake.requests[-1])
    assert msg["From"] == "me@kripya.com" and msg["To"] == "a@x.com, b@y.com" and msg["Cc"] == "c@z.com"
    assert msg["Subject"] == "Plan ✓"
    assert "threadId" not in body_of(fake.requests[-1])


async def test_send_without_a_known_account_omits_from():
    fake = FakeGoogle().on("POST", r"/messages/send$", httpx.Response(200, json={"id": "s1"}))
    ex, _ = fake.executor(Tokens(email=None))
    await ex.execute(USER, "mail.send", {"to": ["a@x.com"], "subject": "s", "body": "b"})
    assert decode_raw(fake.requests[-1])["From"] is None


async def test_draft_creates_a_draft_not_a_send():
    fake = FakeGoogle().on("POST", r"/drafts$", httpx.Response(200, json={"id": "d1", "message": {"id": "m1", "threadId": "t1"}}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.draft", {"to": ["a@x.com"], "subject": "s", "body": "b"})
    assert res.ok and res.data["draftId"] == "d1"
    assert decode_raw(fake.requests[-1])["Subject"] == "s"
    assert not fake.calls("POST", r"/send")


@pytest.mark.parametrize(("orig_subject", "expected"), [("Budget", "Re: Budget"), ("Re: Budget", "Re: Budget")])
async def test_reply_threads_to_the_latest_message(orig_subject, expected):
    older = gmail_message("m1", thread="t7", subject=orig_subject, ts=1000)
    newer = gmail_message("m2", thread="t7", subject=orig_subject, ts=2000,
                          extra_headers=(("References", "<m1@mail.acme.com>"),))
    fake = FakeGoogle().on("GET", r"/threads/t7$", httpx.Response(200, json={"id": "t7", "messages": [newer, older]}))
    fake.on("POST", r"/messages/send$", httpx.Response(200, json={"id": "r1", "threadId": "t7"}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.reply", {"thread_id": "t7", "to": "alice@acme.com", "body": "Yes."})
    assert res.ok and res.data["threadId"] == "t7"
    sent = fake.requests[-1]
    assert body_of(sent)["threadId"] == "t7"
    msg = decode_raw(sent)
    assert msg["Subject"] == expected and msg["To"] == "alice@acme.com" and msg["From"] == "me@kripya.com"
    assert msg["In-Reply-To"] == "<m2@mail.acme.com>"
    assert msg["References"] == "<m1@mail.acme.com> <m2@mail.acme.com>"


async def test_reply_to_an_empty_or_missing_thread_is_not_found():
    fake = FakeGoogle().on("GET", r"/threads/empty$", httpx.Response(200, json={"id": "empty"}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.reply", {"thread_id": "empty", "to": "a@x.com", "body": "b"})
    assert not res.ok and res.error_kind.value == "not_found"


async def test_unsafe_header_text_is_an_invalid_argument_and_nothing_is_sent():
    fake = FakeGoogle().on("POST", r"/messages/send$", httpx.Response(200, json={"id": "s1"}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.send", {"to": ["a@x.com"], "subject": "hi\nBcc: evil@x.com", "body": "b"})
    assert not res.ok and res.error_kind.value == "invalid_argument"
    assert not fake.requests
