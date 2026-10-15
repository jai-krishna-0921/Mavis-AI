# ruff: noqa: E501
import httpx

from mavis.domain.errors import FailureKind as K

from .graphfake import FakeGraph, graph_error, graph_event, graph_message, page
from .test_microsoft_transport import U

BASE = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"


async def test_baseline_walks_pages_publishes_nothing_and_returns_the_delta_link():
    g = FakeGraph().on("GET", r"/inbox/messages/delta$", [
        httpx.Response(200, json=page([{"id": "a"}], next_link=BASE + "?$skiptoken=1")),
        httpx.Response(200, json=page([{"id": "b"}], delta_link=BASE + "?$deltatoken=D1"))])
    ex, _ = g.executor()
    r = await ex.execute(U, "mail.inbox_delta", {"cursor": ""})
    first = g.requests[0].url.params
    assert first["$select"] == "id" and first["$filter"].startswith("receivedDateTime ge ")
    assert r.data["messages"] == [] and r.data["baseline"] is True and r.data["cursor"].endswith("D1")


async def test_incremental_returns_mapped_messages_and_skips_removed():
    g = FakeGraph().on("GET", r"/inbox/messages/delta$", httpx.Response(200, json=page([
        graph_message("m1", folder="F-inbox"), {"id": "gone", "@removed": {"reason": "deleted"}}],
        delta_link=BASE + "?$deltatoken=D2")))
    ex, _ = g.executor()
    r = await ex.execute(U, "mail.inbox_delta", {"cursor": BASE + "?$deltatoken=D1"})
    assert [m["messageId"] for m in r.data["messages"]] == ["ms:m1"] and r.data["cursor"].endswith("D2")
    assert "deltatoken=D1" in str(g.requests[0].url)


async def test_410_resets():
    g = FakeGraph().on("GET", r"/inbox/messages/delta$", graph_error(410, "syncStateNotFound"))
    ex, _ = g.executor()
    r = await ex.execute(U, "mail.inbox_delta", {"cursor": BASE + "?$deltatoken=old"})
    assert r.ok and r.data["reset"] is True and r.data["cursor"] == ""


async def test_a_cursor_for_another_host_is_refused():
    g = FakeGraph()
    ex, _ = g.executor()
    r = await ex.execute(U, "mail.inbox_delta", {"cursor": "https://evil.test/delta"})
    assert r.error_kind is K.INVALID_ARGUMENT and g.requests == []


async def test_more_than_ten_pages_resume_from_the_next_link():
    g = FakeGraph().on("GET", r"/inbox/messages/delta$", httpx.Response(200, json=page(
        [], next_link=BASE + "?$skiptoken=again")))
    ex, _ = g.executor()
    r = await ex.execute(U, "mail.inbox_delta", {"cursor": BASE + "?$deltatoken=D1"})
    assert r.data["more"] is True and r.data["cursor"].endswith("skiptoken=again") and len(g.requests) == 10


async def test_calendar_delta_first_request_carries_the_window_and_removed_become_cancelled():
    cal = "https://graph.microsoft.com/v1.0/me/calendarView/delta"
    g = FakeGraph().on("GET", r"/calendarView/delta$", [
        httpx.Response(200, json=page([], delta_link=cal + "?$deltatoken=C1")),
        httpx.Response(200, json=page([{"id": "e9", "@removed": {"reason": "deleted"}}, graph_event("e1")],
                                      delta_link=cal + "?$deltatoken=C2"))])
    ex, _ = g.executor()
    base = await ex.execute(U, "calendar.delta", {"cursor": ""})
    p = g.requests[0].url.params
    assert "startDateTime" in p and "endDateTime" in p and base.data["items"] == []
    inc = await ex.execute(U, "calendar.delta", {"cursor": base.data["cursor"]})
    assert "startDateTime" not in g.requests[1].url.params
    by = {e["id"]: e for e in inc.data["items"]}
    assert by["ms:e9"]["status"] == "cancelled" and by["ms:e1"]["summary"]
