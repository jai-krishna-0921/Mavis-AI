"""Retry is decided by what a call declares (method + idempotent), never by its URL.

A request is repeated only when repeating cannot do it twice: idempotent calls on a 5xx or a read error,
anything on a 401 or 429 (rejected before it ran), anything that provably never reached Google (connect
errors, pool timeout). A 5xx or a read error on a non-idempotent call is terminal and says the outcome is
unknown."""

from __future__ import annotations

import httpx
import pytest

from mavis.domain.errors import FailureKind, failure_text
from mavis.domain.integrations import UserRef

from .gfake import FakeGoogle, error, gmail_message

USER = UserRef(user_id=3)
SEND = r"/messages/send$"
SENT = httpx.Response(200, json={"id": "m1", "threadId": "t1", "labelIds": ["SENT"]})
MAIL = {"to": ["a@x.com"], "subject": "Hi", "body": "Hello"}
EVENT = {"summary": "Sync", "start": "2026-10-12T10:00:00+05:30", "duration_minutes": 30,
         "attendees": ["a@x.com"]}


def raiser(exc: Exception):
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    return boom


UNSENT = [httpx.ConnectError("down"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")]
MAYBE_SENT = [httpx.ReadTimeout("slow"), httpx.ReadError("reset"), httpx.RemoteProtocolError("cut"),
              httpx.WriteTimeout("slow"), httpx.WriteError("reset")]

# (action, args, route) for every non-idempotent write
WRITES = [
    ("mail.send", MAIL, ("POST", SEND)),
    ("mail.draft", MAIL, ("POST", r"/drafts$")),
    ("calendar.create_event", EVENT, ("POST", r"/calendars/primary/events$")),
]


def fake_for(action: str, route: tuple[str, str], answers) -> FakeGoogle:
    fake = FakeGoogle().on(*route, answers)
    if action == "mail.reply":
        fake.on("GET", r"/threads/t1$", httpx.Response(200, json={"id": "t1", "messages": [
            gmail_message("m0", thread="t1")]}))
    return fake


@pytest.mark.parametrize("exc", MAYBE_SENT, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize(("action", "args", "route"), WRITES, ids=[w[0] for w in WRITES])
async def test_a_write_that_may_have_reached_google_is_sent_exactly_once(action, args, route, exc):
    fake = fake_for(action, route, raiser(exc))
    ex, sleeps = fake.executor()
    res = await ex.execute(USER, action, args)
    assert len(fake.calls(*route)) == 1 and sleeps == []
    assert not res.ok and res.error_kind is FailureKind.UNCONFIRMED
    assert "unknown" in res.error.lower()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize(("action", "args", "route"), WRITES, ids=[w[0] for w in WRITES])
async def test_a_5xx_on_a_write_is_terminal_and_unconfirmed(action, args, route, status):
    fake = fake_for(action, route, error(status, "backendError"))
    ex, sleeps = fake.executor()
    res = await ex.execute(USER, action, args)
    assert len(fake.calls(*route)) == 1 and sleeps == []
    assert res.error_kind is FailureKind.UNCONFIRMED


async def test_reply_is_not_repeated_after_a_read_timeout():
    route = ("POST", SEND)
    fake = fake_for("mail.reply", route, raiser(httpx.ReadTimeout("slow")))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.reply", {"thread_id": "t1", "to": "a@x.com", "body": "ok"})
    assert len(fake.calls(*route)) == 1 and res.error_kind is FailureKind.UNCONFIRMED


@pytest.mark.parametrize("exc", UNSENT, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize(("action", "args", "route"), WRITES, ids=[w[0] for w in WRITES])
async def test_a_write_that_never_reached_google_is_retried(action, args, route, exc):
    fake = fake_for(action, route, [raiser(exc), SENT if action != "calendar.create_event"
                                    else httpx.Response(200, json={"id": "e1"})])
    ex, sleeps = fake.executor()
    res = await ex.execute(USER, action, args)
    assert res.ok and len(fake.calls(*route)) == 2 and len(sleeps) == 1


async def test_a_write_that_never_reaches_google_twice_is_unavailable_not_unconfirmed():
    fake = FakeGoogle().on("POST", SEND, raiser(httpx.ConnectError("down")))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.send", MAIL)
    assert res.error_kind is FailureKind.UNAVAILABLE and len(fake.requests) == 2


@pytest.mark.parametrize("rejected", [error(401, "authError"), error(429, "rateLimitExceeded")],
                         ids=["401", "429"])
async def test_a_rejected_write_is_retried(rejected):
    fake = FakeGoogle().on("POST", SEND, [rejected, SENT])
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.send", MAIL)
    assert res.ok and len(fake.requests) == 2


@pytest.mark.parametrize("exc", UNSENT + MAYBE_SENT, ids=lambda e: type(e).__name__)
async def test_a_read_is_retried_after_any_transport_error(exc):
    ok = httpx.Response(200, json={"emailAddress": "me@orbit.test"})
    fake = FakeGoogle().on("GET", r"/profile$", [raiser(exc), ok])
    ex, sleeps = fake.executor()
    res = await ex.execute(USER, "mail.profile", {})
    assert res.ok and len(fake.requests) == 2 and len(sleeps) == 1


async def test_a_read_5xx_is_retried_then_unavailable():
    fake = FakeGoogle().on("GET", r"/profile$", error(503, "backendError"))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.profile", {})
    assert res.error_kind is FailureKind.UNAVAILABLE and len(fake.requests) == 2


async def test_a_declared_idempotent_post_is_retried():
    """freeBusy is a POST that only reads: the call declares it, the URL does not matter."""
    ok = httpx.Response(200, json={"calendars": {"primary": {"busy": []}}})
    fake = FakeGoogle().on("POST", r"/freeBusy$", [error(503, "backendError"), ok])
    ex, _ = fake.executor()
    res = await ex.execute(USER, "calendar.free_slots", {
        "time_min": "2026-10-12T09:00:00+05:30", "time_max": "2026-10-12T17:00:00+05:30"})
    assert res.ok and len(fake.requests) == 2


async def test_a_patch_that_notifies_attendees_is_not_repeated_but_a_silent_one_is():
    route = ("PATCH", r"/events/e1$")
    existing = httpx.Response(200, json={"attendees": []})
    notifying = FakeGoogle().on("GET", r"/events/e1$", existing).on(*route, error(503, "backendError"))
    ex, _ = notifying.executor()
    res = await ex.execute(USER, "calendar.update_event", {"event_id": "e1", "attendees": ["a@x.com"]})
    assert len(notifying.calls(*route)) == 1 and res.error_kind is FailureKind.UNCONFIRMED
    silent = FakeGoogle().on(*route, [error(503, "backendError"), httpx.Response(200, json={"id": "e1"})])
    ex, _ = silent.executor()
    res = await ex.execute(USER, "calendar.update_event", {"event_id": "e1", "summary": "New"})
    assert res.ok and len(silent.calls(*route)) == 2


def test_unconfirmed_text_is_honest_and_dash_free():
    text = failure_text(FailureKind.UNCONFIRMED, "Gmail")
    assert "may or may not" in text and "Check" in text and "—" not in text and "–" not in text
