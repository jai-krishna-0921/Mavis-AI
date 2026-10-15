import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from mavis.domain.errors import FailureKind as K
from mavis.tools.integrations.normalize import normalize_calendar_event, to_datetime

from .graphfake import FakeGraph, graph_error, graph_event, page, query_of
from .test_microsoft_transport import U

WIN = {"time_min": "2026-10-15T00:00:00Z", "time_max": "2026-10-16T00:00:00Z"}
AFTERNOON = {"time_min": "2026-10-15T09:00:00Z", "time_max": "2026-10-15T14:00:00Z"}


def test_seven_digit_fraction_parses():
    assert to_datetime("2026-10-06T09:00:00.0000000") == datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


async def test_list_uses_calendar_view_utc_and_maps_to_google_keys():
    g = FakeGraph().on("GET", r"/v1.0/me/calendar/calendarView$", httpx.Response(200, json=page([
        graph_event("e1", subject="Standup", start="2026-10-15T09:00:00.0000000",
                    end="2026-10-15T09:30:00.0000000",
                    attendees=(("Bob", "bob@acme.com", "accepted"),), is_online=True)])))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.list", {**WIN})
    req = g.calls("GET", "calendarView")[0]
    q = query_of(req)
    assert q["startDateTime"] == "2026-10-15T00:00:00Z" and q["endDateTime"] == "2026-10-16T00:00:00Z"
    assert q["$orderby"] == "start/dateTime" and q["$top"] == "20"
    assert 'outlook.timezone="UTC"' in req.headers["prefer"]
    assert 'IdType="ImmutableId"' in req.headers["prefer"]
    ev = r.data["items"][0]
    assert ev["id"] == "ms:e1" and ev["summary"] == "Standup"
    assert ev["attendees"][0]["email"] == "bob@acme.com"
    assert ev["start"] == {"dateTime": "2026-10-15T09:00:00+00:00", "timeZone": "UTC"}
    n = normalize_calendar_event(ev)
    assert n["event_id"] == "ms:e1" and n["start"].startswith("2026-10-15T09:00")
    assert n["attendees"] == ["bob@acme.com"]
    assert ev["hangoutLink"].startswith("https://")
    g.assert_all_routed()


async def test_updated_min_is_applied_locally_and_cancelled_events_are_kept():
    g = FakeGraph().on("GET", "calendarView", httpx.Response(200, json=page([
        graph_event("old", modified="2026-10-01T00:00:00Z"),
        graph_event("new", modified="2026-10-14T00:00:00Z", cancelled=True)])))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.list", {**WIN, "updated_min": "2026-10-10T00:00:00Z"})
    assert [e["id"] for e in r.data["items"]] == ["ms:new"] and r.data["items"][0]["status"] == "cancelled"
    assert "$filter" not in g.calls("GET", "calendarView")[0].url.params
    assert any("locally" in n for n in r.data["notes"])


async def test_secondary_calendar_uses_its_own_path_and_pages_follow_links_with_prefer():
    nxt = "https://graph.microsoft.com/v1.0/me/calendars/CAL1=/calendarView?$skiptoken=abc"
    g = FakeGraph()
    g.on("GET", r"calendars/CAL1=/calendarView$", [
        httpx.Response(200, json=page([graph_event("a")], next_link=nxt)),
        httpx.Response(200, json=page([graph_event("b")]))])
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.list", {**WIN, "calendar_id": "ms:CAL1=", "max_results": 50})
    assert [e["id"] for e in r.data["items"]] == ["ms:a", "ms:b"]
    assert all('outlook.timezone="UTC"' in q.headers["prefer"] for q in g.requests)
    g.assert_all_routed()


async def test_event_timezones_dst_and_all_day():
    """UTC answers parse as UTC across the London DST end; a named zone is honoured; all-day is a date."""
    g = FakeGraph().on("GET", "calendarView", httpx.Response(200, json=page([
        graph_event("before", start="2026-10-24T09:00:00", end="2026-10-24T10:00:00"),
        graph_event("after", start="2026-10-25T09:00:00", end="2026-10-25T10:00:00"),
        graph_event("london_bst", start="2026-10-24T09:00:00", end="2026-10-24T10:00:00", tz="Europe/London"),
        graph_event("london_gmt", start="2026-10-25T09:00:00", end="2026-10-25T10:00:00", tz="Europe/London"),
        graph_event("day", start="2026-10-25", end="2026-10-26", all_day=True),
    ])))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.list", {
        "time_min": "2026-10-24T00:00:00Z", "time_max": "2026-10-27T00:00:00Z"})
    by = {e["id"]: e for e in r.data["items"]}
    got = {k: to_datetime(v["start"]) for k, v in by.items() if "dateTime" in v["start"]}
    assert got["ms:before"] == datetime(2026, 10, 24, 9, tzinfo=UTC)
    assert got["ms:after"] == datetime(2026, 10, 25, 9, tzinfo=UTC)
    assert got["ms:london_bst"] == datetime(2026, 10, 24, 8, tzinfo=UTC)  # BST is UTC+1
    assert got["ms:london_gmt"] == datetime(2026, 10, 25, 9, tzinfo=UTC)  # after the change: UTC+0
    assert by["ms:day"]["start"] == {"date": "2026-10-25"} and by["ms:day"]["end"] == {"date": "2026-10-26"}
    assert normalize_calendar_event(by["ms:day"])["start"].startswith("2026-10-25")


async def test_free_slots_come_from_busy_spans():
    g = FakeGraph().on("GET", "calendarView", httpx.Response(200, json=page([
        graph_event("a", start="2026-10-15T10:00:00", end="2026-10-15T11:00:00", show_as="busy"),
        graph_event("b", start="2026-10-15T12:00:00", end="2026-10-15T13:00:00", show_as="free"),
        graph_event("c", start="2026-10-15T13:00:00", end="2026-10-15T13:30:00", show_as="busy",
                    cancelled=True)])))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.free_slots", AFTERNOON)
    flat = json.dumps(r.data)
    assert "10:00:00" in flat and "11:00:00" in flat and r.ok
    assert len(r.data["busy"]) == 1
    assert [(s["start"], s["end"]) for s in r.data["free_slots"]] == [
        ("2026-10-15T09:00:00+00:00", "2026-10-15T10:00:00+00:00"),
        ("2026-10-15T11:00:00+00:00", "2026-10-15T14:00:00+00:00")]


async def test_free_slots_all_day_busy_event_blocks_the_day_and_free_one_does_not():
    g = FakeGraph().on("GET", "calendarView", httpx.Response(200, json=page([
        graph_event("oof", start="2026-10-15", end="2026-10-16", all_day=True, show_as="oof"),
        graph_event("fyi", start="2026-10-15", end="2026-10-16", all_day=True, show_as="free")])))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.free_slots", AFTERNOON)
    assert r.data["free_slots"] == [] and len(r.data["busy"]) == 1


async def test_find_matches_title_place_and_guests_locally():
    g = FakeGraph().on("GET", "calendarView", httpx.Response(200, json=page([
        graph_event("t", subject="Budget review"),
        graph_event("p", subject="Lunch", location="Cafe Roma"),
        graph_event("g", subject="Sync", attendees=(("Bob", "bob@acme.com", "accepted"),)),
        graph_event("n", subject="Other")])))
    ex, _ = g.executor()
    ids = {}
    for q in ("BUDGET", "roma", "bob@acme"):
        r = await ex.execute(U, "calendar.find", {"query": q})
        ids[q] = [e["id"] for e in r.data["items"]]
    assert ids == {"BUDGET": ["ms:t"], "roma": ["ms:p"], "bob@acme": ["ms:g"]}
    assert "title, place and guests" in r.data["notes"][0]
    q = query_of(g.calls("GET", "calendarView")[0])
    start, end = to_datetime(q["startDateTime"]), to_datetime(q["endDateTime"])
    assert 179 <= (end - datetime.now(UTC)).days <= 180 and 29 <= (datetime.now(UTC) - start).days <= 30


async def test_create_sends_utc_and_is_not_repeated_after_a_read_error():
    seen = []

    def ok(request):
        seen.append(json.loads(request.content))
        return httpx.Response(201, json=graph_event("new1"))
    g = FakeGraph().on("POST", r"/v1.0/me/events$", ok)
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.create_event", {
        "summary": "Sync", "start": "2026-10-20T09:00:00Z", "duration_minutes": 30, "attendees": ["a@b.com"]})
    assert r.ok and r.data["id"] == "ms:new1"
    assert seen[0]["start"] == {"dateTime": "2026-10-20T09:00:00.0000000", "timeZone": "UTC"}
    assert seen[0]["end"] == {"dateTime": "2026-10-20T09:30:00.0000000", "timeZone": "UTC"}
    assert seen[0]["attendees"][0]["emailAddress"]["address"] == "a@b.com"

    def boom(request):
        raise httpx.ReadError("x")
    g2 = FakeGraph().on("POST", r"/v1.0/me/events$", boom)
    ex2, _ = g2.executor()
    r2 = await ex2.execute(U, "calendar.create_event", {"summary": "S", "start": "2026-10-20T09:00:00Z"})
    assert r2.error_kind is K.UNCONFIRMED and len(g2.requests) == 1


async def test_create_converts_a_named_zone_and_measures_the_length_in_elapsed_time():
    """00:30 BST on the night the clocks go back, two hours long: ends 01:30 UTC, not 02:30 wall clock."""
    seen = []

    def ok(request):
        seen.append(json.loads(request.content))
        return httpx.Response(201, json=graph_event("n"))
    g = FakeGraph().on("POST", r"/v1.0/me/events$", ok)
    ex, _ = g.executor()
    london = ZoneInfo("Europe/London")
    r = await ex.execute(U, "calendar.create_event", {
        "summary": "Night", "start": datetime(2026, 10, 25, 0, 30, tzinfo=london), "duration_minutes": 120,
        "description": "bring it", "location": "HQ"})
    assert r.ok
    assert seen[0]["start"]["dateTime"] == "2026-10-24T23:30:00.0000000"
    assert seen[0]["end"]["dateTime"] == "2026-10-25T01:30:00.0000000"
    assert seen[0]["location"] == {"displayName": "HQ"} and seen[0]["body"]["content"] == "bring it"
    assert "attendees" not in seen[0]


async def test_update_keeps_existing_guest_responses():
    g = FakeGraph()
    g.on("GET", r"/v1.0/me/events/e1$", httpx.Response(200, json=graph_event(
        "e1", attendees=(("Bob", "bob@acme.com", "accepted"),))))
    g.on("PATCH", r"/v1.0/me/events/e1$", httpx.Response(200, json=graph_event("e1")))
    ex, _ = g.executor()
    await ex.execute(U, "calendar.update_event", {
        "event_id": "ms:e1", "attendees": ["bob@acme.com", "c@d.com"]})
    body = json.loads(g.calls("PATCH", "events/e1")[0].content)
    by = {a["emailAddress"]["address"]: a for a in body["attendees"]}
    assert by["bob@acme.com"]["status"]["response"] == "accepted" and "c@d.com" in by


async def test_update_without_guests_is_repeatable_and_with_guests_is_not():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return graph_error(500) if calls["n"] == 1 else httpx.Response(200, json=graph_event("e1"))
    g = FakeGraph().on("PATCH", r"/v1.0/me/events/e1$", flaky)
    ex, sleeps = g.executor()
    ok = await ex.execute(U, "calendar.update_event", {"event_id": "e1", "summary": "New"})
    assert ok.ok and calls["n"] == 2 and sleeps
    g2 = FakeGraph()
    g2.on("GET", r"/v1.0/me/events/e1$", httpx.Response(200, json=graph_event("e1")))
    g2.on("PATCH", r"/v1.0/me/events/e1$", graph_error(500))
    ex2, _ = g2.executor()
    r = await ex2.execute(U, "calendar.update_event", {"event_id": "e1", "attendees": ["x@y.com"]})
    assert r.error_kind is K.UNCONFIRMED and len(g2.calls("PATCH", "e1")) == 1


async def test_update_moves_in_utc():
    g = FakeGraph().on("PATCH", r"/v1.0/me/events/e1$", httpx.Response(200, json=graph_event("e1")))
    ex, _ = g.executor()
    await ex.execute(U, "calendar.update_event", {
        "event_id": "e1", "start": "2026-03-29T10:00:00+01:00", "duration_minutes": 60})
    body = json.loads(g.requests[0].content)
    assert body["start"]["dateTime"] == "2026-03-29T09:00:00.0000000"
    assert body["end"]["dateTime"] == "2026-03-29T10:00:00.0000000"


async def test_get_maps_one_event_with_self_and_organiser_flags():
    g = FakeGraph().on("GET", r"/v1.0/me/events/e1$", httpx.Response(200, json=graph_event(
        "e1", attendees=(("Me", "ME@orbit.test", "tentativelyAccepted"), ("Bob", "bob@acme.com", "none"),
                         ("Carl", "carl@acme.com", "declined")),
        organizer="Alice <alice@acme.com>", series_master="s1", location="Room 4")))
    ex, _ = g.executor()
    ev = (await ex.execute(U, "calendar.get", {"event_id": "ms:e1"})).data
    states = {a["email"]: (a["responseStatus"], a["self"]) for a in ev["attendees"]}
    assert states == {"ME@orbit.test": ("tentative", True), "bob@acme.com": ("needsAction", False),
                      "carl@acme.com": ("declined", False)}
    assert ev["organizer"] == {"email": "alice@acme.com", "self": False}
    assert ev["recurringEventId"] == "ms:s1" and ev["location"] == "Room 4" and ev["status"] == "confirmed"
    assert ev["iCalUId"] == "ical-e1" and ev["updated"] and ev["htmlLink"].startswith("https://")


async def test_delete_organiser_meeting_needs_notify_and_uses_cancel():
    g = FakeGraph().on("GET", r"/v1.0/me/events/e1$", httpx.Response(200, json=graph_event(
        "e1", attendees=(("Bob", "bob@acme.com", "accepted"),), organizer_is_me=True)))
    g.on("POST", r"/v1.0/me/events/e1/cancel$", httpx.Response(202))
    ex, _ = g.executor()
    refused = await ex.execute(U, "calendar.delete_event", {"event_id": "ms:e1"})
    assert refused.error_kind is K.INVALID_ARGUMENT
    assert not g.calls("DELETE", ".*") and not g.calls("POST", "cancel")
    ok = await ex.execute(U, "calendar.delete_event", {"event_id": "ms:e1", "notify_guests": True})
    assert ok.ok and ok.data["guestsNotified"] is True and len(g.calls("POST", "/cancel$")) == 1
    assert json.loads(g.calls("POST", "/cancel$")[0].content) == {"comment": ""}


async def test_delete_cancel_is_not_repeated_after_an_unknown_outcome():
    g = FakeGraph().on("GET", r"/v1.0/me/events/e1$", httpx.Response(200, json=graph_event(
        "e1", attendees=(("Bob", "bob@acme.com", "accepted"),), organizer_is_me=True)))
    g.on("POST", r"/cancel$", graph_error(500))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.delete_event", {"event_id": "e1", "notify_guests": True})
    assert r.error_kind is K.UNCONFIRMED and len(g.calls("POST", "/cancel$")) == 1


async def test_delete_solo_event_uses_delete():
    g = FakeGraph().on("GET", r"/v1.0/me/events/e2$", httpx.Response(
        200, json=graph_event("e2", organizer_is_me=True)))
    g.on("DELETE", r"/v1.0/me/events/e2$", httpx.Response(204))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.delete_event", {"event_id": "e2"})
    assert r.ok and r.data == {"id": "e2", "deleted": True, "guestsNotified": False}


async def test_delete_an_invitation_uses_delete_even_with_guests():
    g = FakeGraph().on("GET", r"/v1.0/me/events/e4$", httpx.Response(200, json=graph_event(
        "e4", attendees=(("Me", "me@orbit.test", "accepted"), ("Bob", "bob@acme.com", "accepted")))))
    g.on("DELETE", r"/v1.0/me/events/e4$", httpx.Response(204))
    ex, _ = g.executor()
    assert (await ex.execute(U, "calendar.delete_event", {"event_id": "e4"})).ok
    assert not g.calls("POST", "cancel")


async def test_respond_maps_google_words_to_graph_actions():
    g = FakeGraph().on("POST", r"/v1.0/me/events/e3/(accept|tentativelyAccept|decline)$", httpx.Response(202))
    ex, _ = g.executor()
    for word in ("accepted", "tentative", "declined"):
        done = await ex.execute(U, "calendar.respond", {
            "event_id": "ms:e3", "response": word, "comment": "ok"})
        assert done.ok
    verbs = [r.url.path.rsplit("/", 1)[1] for r in g.calls("POST", "/e3/")]
    assert verbs == ["accept", "tentativelyAccept", "decline"]
    assert json.loads(g.calls("POST", "/e3/")[0].content) == {"comment": "ok", "sendResponse": True}


async def test_respond_on_own_event_says_the_user_is_not_a_guest_and_is_not_repeated():
    g = FakeGraph().on("POST", r"/accept$", graph_error(400, "ErrorInvalidRequest"))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.respond", {"event_id": "e5", "response": "accepted"})
    assert r.error_kind is K.INVALID_ARGUMENT and "not a guest" in r.error
    g2 = FakeGraph().on("POST", r"/accept$", graph_error(500))
    ex2, _ = g2.executor()
    r2 = await ex2.execute(U, "calendar.respond", {"event_id": "e5", "response": "accepted"})
    assert r2.error_kind is K.UNCONFIRMED and len(g2.requests) == 1


async def test_calendars_and_secondary_calendar_ids_are_validated():
    g = FakeGraph().on("GET", r"/v1.0/me/calendars$", httpx.Response(200, json=page([
        {"id": "CAL1=", "name": "Calendar", "isDefaultCalendar": True, "canEdit": True},
        {"id": "CAL2=", "name": "Team", "isDefaultCalendar": False, "canEdit": False}])))
    ex, _ = g.executor()
    r = await ex.execute(U, "calendar.calendars", {})
    cals = r.data["calendars"]
    assert cals[0]["primary"] is True and cals[0]["id"] == "ms:CAL1=" and cals[0]["accessRole"] == "owner"
    assert cals[1]["accessRole"] == "reader" and cals[1]["summary"] == "Team"
    bad = await ex.execute(U, "calendar.list", {**WIN, "calendar_id": "x/../y"})
    assert bad.error_kind is K.INVALID_ARGUMENT
