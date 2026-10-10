"""Calendar, Drive, Docs, Sheets and People against fake APIs, via the existing normalizers and renderers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from mavis.domain.integrations import UserRef
from mavis.tools.integrations import workspace_render as wr
from mavis.tools.integrations.normalize import (
    calendar_event,
    extract_calendar_items,
    extract_list,
    normalize_calendar_event,
)
from mavis.tools.integrations.workspace_guard import ownership

from .gfake import FakeGoogle, body_of, error

USER = UserRef(user_id=5)
EVENTS = r"/calendar/v3/calendars/primary/events$"
EVENT = r"/calendar/v3/calendars/primary/events/[^/]+$"


def event(eid: str, summary: str = "Standup", **kw) -> dict:
    return {
        "id": eid,
        "status": "confirmed",
        "summary": summary,
        "updated": "2026-10-05T04:00:00.000Z",
        "start": {"dateTime": "2026-10-06T10:00:00+05:30", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": "2026-10-06T10:30:00+05:30"},
        "attendees": [{"email": "a@x.com", "responseStatus": "accepted"}],
        **kw,
    }


# --- calendar -----------------------------------------------------------------------------------------


async def test_calendar_list_params_pagination_and_normalizer():
    pages = {
        None: {"items": [event("e1"), event("e2")], "nextPageToken": "N", "timeZone": "Asia/Kolkata"},
        "N": {"items": [event("e3")]},
    }
    fake = FakeGoogle().on(
        "GET", EVENTS, lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken")])
    )
    ex, _ = fake.executor()
    res = await ex.execute(
        USER,
        "calendar.list",
        {"time_min": "2026-10-05T00:00:00+05:30", "time_max": "2026-10-12T00:00:00+05:30", "max_results": 3},
    )
    assert res.ok
    q = fake.requests[0].url.params
    assert q["singleEvents"] == "true" and q["orderBy"] == "startTime"
    assert q["timeMin"] == "2026-10-05T00:00:00+05:30" and "updatedMin" not in q and "showDeleted" not in q
    items = extract_calendar_items(res.data)
    assert [i["id"] for i in items] == ["e1", "e2", "e3"]
    ev = calendar_event(5, items[0], "poller")
    assert ev.payload["summary"] == "Standup" and ev.payload["attendees"] == ["a@x.com"]
    assert ev.payload["start"].startswith("2026-10-06T10:00:00")


async def test_calendar_list_with_updated_min_includes_cancelled_events():
    cancelled = {"id": "gone", "status": "cancelled", "updated": "2026-10-05T05:00:00Z"}
    fake = FakeGoogle().on("GET", EVENTS, httpx.Response(200, json={"items": [cancelled]}))
    ex, _ = fake.executor()
    res = await ex.execute(
        USER,
        "calendar.list",
        {
            "time_min": "2026-10-05T00:00:00Z",
            "time_max": "2026-11-05T00:00:00Z",
            "updated_min": "2026-10-05T04:30:00Z",
            "max_results": 50,
        },
    )
    q = fake.requests[0].url.params
    assert q["updatedMin"] == "2026-10-05T04:30:00+00:00" and q["showDeleted"] == "true"
    assert normalize_calendar_event(extract_calendar_items(res.data)[0])["status"] == "cancelled"


async def test_naive_times_are_treated_as_utc():
    fake = FakeGoogle().on("GET", EVENTS, httpx.Response(200, json={"items": []}))
    ex, _ = fake.executor()
    await ex.execute(
        USER, "calendar.list", {"time_min": "2026-10-05T00:00:00", "time_max": "2026-10-06T00:00:00"}
    )
    assert fake.requests[0].url.params["timeMin"].endswith("+00:00")


async def test_calendar_find_searches_text():
    fake = FakeGoogle().on("GET", EVENTS, httpx.Response(200, json={"items": [event("e9", "Dentist")]}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "calendar.find", {"query": "dentist"})
    assert fake.requests[0].url.params["q"] == "dentist" and res.data["items"][0]["summary"] == "Dentist"


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        ("2026-10-06T15:00:00+05:30", {"dateTime": "2026-10-06T15:00:00+05:30"}),
        ("2026-10-06T15:00:00Z", {"dateTime": "2026-10-06T15:00:00+00:00"}),
        ("2026-10-06T15:00:00", {"dateTime": "2026-10-06T15:00:00", "timeZone": "UTC"}),
    ],
)
async def test_create_event_with_guests_sends_invites(start, expected):
    fake = FakeGoogle().on("POST", EVENTS, lambda r: httpx.Response(200, json={"id": "new", **body_of(r)}))
    ex, _ = fake.executor()
    res = await ex.execute(
        USER,
        "calendar.create_event",
        {
            "summary": "Sync",
            "start": start,
            "duration_minutes": 45,
            "attendees": ["a@x.com", "b@y.com"],
            "description": "agenda",
        },
    )
    assert res.ok
    req = fake.requests[0]
    body = body_of(req)
    assert req.url.params["sendUpdates"] == "all"
    assert body["start"] == expected and body["description"] == "agenda"
    assert body["attendees"] == [{"email": "a@x.com"}, {"email": "b@y.com"}]
    s, e = (datetime.fromisoformat(body[k]["dateTime"]) for k in ("start", "end"))
    assert (e - s).total_seconds() == 45 * 60


async def test_create_event_without_guests_sends_no_updates_and_no_empty_fields():
    fake = FakeGoogle().on("POST", EVENTS, httpx.Response(200, json={"id": "new"}))
    ex, _ = fake.executor()
    await ex.execute(
        USER,
        "calendar.create_event",
        {"summary": "Focus", "start": "2026-10-06T09:00:00+05:30", "duration_minutes": 60},
    )
    req = fake.requests[0]
    assert "sendUpdates" not in req.url.params
    assert set(body_of(req)) == {"summary", "start", "end"}


async def test_event_location_is_set_on_create_and_update():
    fake = (FakeGoogle().on("POST", EVENTS, httpx.Response(200, json={"id": "new"}))
            .on("PATCH", EVENT, httpx.Response(200, json=event("e1"))))
    ex, _ = fake.executor()
    await ex.execute(USER, "calendar.create_event", {
        "summary": "Interview", "start": "2026-10-11T15:00:00+05:30", "location": "Chennai"})
    await ex.execute(USER, "calendar.update_event", {"event_id": "e1", "location": "Bengaluru office"})
    assert body_of(fake.requests[0])["location"] == "Chennai"
    assert body_of(fake.requests[1]) == {"location": "Bengaluru office"}


def test_event_previews_show_the_location():
    from mavis.tools.integrations.actions import ACTIONS, CalendarCreateArgs, CalendarUpdateArgs

    made = CalendarCreateArgs(summary="Interview", start="2026-10-11T15:00:00+05:30", location="Chennai",
                              attendees=["p@example.com"])
    assert "Where: Chennai" in ACTIONS["calendar.create_event"].preview(made, "Asia/Kolkata")
    moved = CalendarUpdateArgs(event_id="e1", location="Chennai")
    assert "Where: Chennai" in ACTIONS["calendar.update_event"].preview(moved, "Asia/Kolkata")


async def test_update_event_patches_only_the_changed_fields():
    fake = FakeGoogle().on("PATCH", EVENT, httpx.Response(200, json=event("e1", "New")))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "calendar.update_event", {"event_id": "e1", "summary": "New"})
    assert res.ok and body_of(fake.requests[0]) == {"summary": "New"}
    assert "sendUpdates" not in fake.requests[0].url.params and len(fake.requests) == 1


async def test_update_event_move_sends_start_and_end_and_description_can_be_cleared():
    fake = FakeGoogle().on("PATCH", EVENT, httpx.Response(200, json=event("e1")))
    ex, _ = fake.executor()
    await ex.execute(
        USER,
        "calendar.update_event",
        {"event_id": "e1", "start": "2026-10-07T11:00:00+05:30", "duration_minutes": 30, "description": ""},
    )
    body = body_of(fake.requests[0])
    assert body["start"] == {"dateTime": "2026-10-07T11:00:00+05:30"}
    assert body["end"] == {"dateTime": "2026-10-07T11:30:00+05:30"} and body["description"] == ""


async def test_update_event_attendees_keep_existing_responses_and_notify():
    existing = event(
        "e1",
        attendees=[
            {"email": "A@x.com", "responseStatus": "accepted"},
            {"email": "old@x.com", "responseStatus": "declined"},
        ],
    )
    fake = FakeGoogle().on("GET", EVENT, httpx.Response(200, json=existing))
    fake.on("PATCH", EVENT, httpx.Response(200, json=existing))
    ex, _ = fake.executor()
    await ex.execute(USER, "calendar.update_event", {"event_id": "e1", "attendees": ["a@x.com", "new@y.com"]})
    patch = fake.calls("PATCH", EVENT)[0]
    assert body_of(patch)["attendees"] == [
        {"email": "A@x.com", "responseStatus": "accepted"},
        {"email": "new@y.com"},
    ]
    assert patch.url.params["sendUpdates"] == "all"


@pytest.mark.parametrize(
    ("busy", "free"),
    [
        ([], [("10:00", "18:00")]),
        (
            [("11:00", "12:00"), ("14:00", "15:30")],
            [("10:00", "11:00"), ("12:00", "14:00"), ("15:30", "18:00")],
        ),
        (
            [("09:00", "11:00"), ("10:30", "12:00"), ("17:50", "19:00")],
            [("12:00", "17:50")],
        ),  # overlap, clipped
        ([("10:00", "18:00")], []),
        ([("10:00", "10:50"), ("11:00", "18:00")], []),  # a 10 minute gap is not a slot
    ],
)
async def test_free_slots_are_the_gaps_between_busy_spans(busy, free):
    def utc(hhmm: str) -> str:  # IST wall clock to UTC for Google's reply
        h, m = map(int, hhmm.split(":"))
        total = h * 60 + m - 330
        return f"2026-10-06T{total // 60:02d}:{total % 60:02d}:00Z" if total >= 0 else "2026-10-05T23:00:00Z"

    reply = {"calendars": {"primary": {"busy": [{"start": utc(s), "end": utc(e)} for s, e in busy]}}}
    fake = FakeGoogle().on("POST", r"/freeBusy$", httpx.Response(200, json=reply))
    ex, _ = fake.executor()
    res = await ex.execute(
        USER,
        "calendar.free_slots",
        {"time_min": "2026-10-06T10:00:00+05:30", "time_max": "2026-10-06T18:00:00+05:30"},
    )
    assert res.ok
    assert body_of(fake.requests[0])["items"] == [{"id": "primary"}]
    got = [(s["start"][11:16], s["end"][11:16]) for s in res.data["free_slots"]]
    assert got == free
    assert all(s["start"].endswith("+05:30") for s in res.data["free_slots"])


# --- drive --------------------------------------------------------------------------------------------

FILES = r"/drive/v3/files$"
FILE = r"/drive/v3/files/[^/]+$"
DOC_MIME = "application/vnd.google-apps.document"


def drive_file(fid: str, name: str, mime: str = DOC_MIME) -> dict:
    return {
        "id": fid,
        "name": name,
        "mimeType": mime,
        "modifiedTime": "2026-10-04T08:00:00.000Z",
        "owners": [{"displayName": "Priya", "emailAddress": "priya@x.com", "me": False}],
    }


async def test_drive_search_builds_q_and_orders_unless_fulltext():
    fake = FakeGoogle().on("GET", FILES, httpx.Response(200, json={"files": [drive_file("f1", "Budget")]}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "drive.search", {"query": "name contains 'budget'", "max_results": 5})
    q = fake.requests[-1].url.params
    assert q["q"] == "(name contains 'budget') and trashed = false" and q["orderBy"] == "modifiedTime desc"
    assert q["pageSize"] == "5" and "owners" in q["fields"]
    assert "file_id=f1 | Budget | Doc | owner: Priya" in wr.render_files(res.data)
    await ex.execute(USER, "drive.search", {"query": "fullText contains 'Priya'"})
    assert "orderBy" not in fake.requests[-1].url.params
    await ex.execute(USER, "drive.search", {})
    assert fake.requests[-1].url.params["q"] == "trashed = false"


async def test_drive_recent_and_shared_with_me():
    fake = FakeGoogle().on(
        "GET",
        FILES,
        httpx.Response(
            200,
            json={
                "files": [
                    {
                        **drive_file("f2", "Plan"),
                        "sharedWithMeTime": "2026-10-03T00:00:00Z",
                        "sharingUser": {"displayName": "Raj"},
                    }
                ]
            },
        ),
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "drive.list_recent", {"shared_with_me": True, "max_results": 10})
    q = fake.requests[-1].url.params
    assert q["q"] == "sharedWithMe and trashed = false" and q["orderBy"] == "sharedWithMeTime desc"
    assert "shared with you 2026-10-03 by Raj" in wr.render_files(res.data)
    await ex.execute(USER, "drive.list_recent", {})
    assert fake.requests[-1].url.params["orderBy"] == "modifiedTime desc"


async def test_drive_pagination_reaches_the_limit():
    pages = {
        None: {"files": [drive_file("a", "A"), drive_file("b", "B")], "nextPageToken": "T"},
        "T": {"files": [drive_file("c", "C"), drive_file("d", "D")], "nextPageToken": "U"},
    }
    fake = FakeGoogle().on(
        "GET", FILES, lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken")])
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "drive.search", {"query": "x", "max_results": 3})
    assert [f["id"] for f in res.data["files"]] == ["a", "b", "c"] and res.data["nextPageToken"] == "U"


async def test_meta_and_permissions_feed_the_ownership_rule():
    meta = {**drive_file("f1", "Plan"), "ownedByMe": True}
    perms = {
        "permissions": [
            {"id": "1", "type": "user", "role": "owner", "emailAddress": "me@kripya.com"},
            {"id": "2", "type": "user", "role": "reader", "emailAddress": "x@y.com"},
        ]
    }
    fake = FakeGoogle().on("GET", FILE, httpx.Response(200, json=meta))
    fake.on("GET", r"/permissions$", httpx.Response(200, json=perms))
    ex, _ = fake.executor()
    m = await ex.execute(USER, "drive.meta", {"file_id": "f1"})
    p = await ex.execute(USER, "drive.permissions", {"file_id": "f1"})
    assert m.data["name"] == "Plan" and m.data["mimeType"] == DOC_MIME
    assert ownership(extract_list(p.data, "permissions"), "me@kripya.com") == (True, True)
    assert "emailAddress" in fake.requests[-1].url.params["fields"]


@pytest.mark.parametrize(
    ("mime", "default_export", "content_type", "payload", "expected"),
    [
        (DOC_MIME, "text/plain", "text/plain; charset=utf-8", "Doc text ✓".encode(), "Doc text ✓"),
        ("application/vnd.google-apps.spreadsheet", "text/csv", "text/csv", b"a,b\n1,2", "a,b\n1,2"),
        ("application/vnd.google-apps.presentation", "text/plain", "text/plain", b"Slide 1", "Slide 1"),
    ],
)
async def test_download_exports_google_files_to_text(mime, default_export, content_type, payload, expected):
    fake = FakeGoogle().on("GET", FILE, httpx.Response(200, json=drive_file("f", "N", mime)))
    fake.on("GET", r"/export$", httpx.Response(200, content=payload, headers={"content-type": content_type}))
    ex, _ = fake.executor()
    for action in ("drive.download", "drive.read"):
        res = await ex.execute(USER, action, {"file_id": "f"})
        assert (
            res.ok and res.data["text"] == expected and res.data["name"] == "N" and not res.data["truncated"]
        )
        assert fake.calls("GET", r"/export$")[-1].url.params["mimeType"] == default_export


async def test_export_file_returns_the_exported_bytes_and_refuses_non_google_files():
    import base64

    pdf = "application/pdf"
    fake = FakeGoogle().on("GET", FILE, httpx.Response(200, json=drive_file("f", "Deck",
                                                                       "application/vnd.google-apps.presentation")))
    fake.on("GET", r"/export$", httpx.Response(200, content=b"%PDF-1.7 bytes", headers={"content-type": pdf}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "drive.export_file", {"file_id": "f", "mime_type": pdf})
    assert res.ok and base64.b64decode(res.data["content_b64"]) == b"%PDF-1.7 bytes"
    assert res.data["name"] == "Deck" and fake.calls("GET", r"/export$")[0].url.params["mimeType"] == pdf

    plain = FakeGoogle().on("GET", FILE, httpx.Response(200, json=drive_file("g", "scan.pdf", pdf)))
    ex, _ = plain.executor()
    res = await ex.execute(USER, "drive.export_file", {"file_id": "g", "mime_type": pdf})
    assert not res.ok and not plain.calls("GET", r"/export$")


async def test_download_honours_the_requested_export_type():
    fake = FakeGoogle().on("GET", FILE, httpx.Response(200, json=drive_file("f", "N")))
    fake.on(
        "GET", r"/export$", httpx.Response(200, content=b"<p>x</p>", headers={"content-type": "text/html"})
    )
    ex, _ = fake.executor()
    await ex.execute(USER, "drive.download", {"file_id": "f", "mime_type": "text/html"})
    assert fake.calls("GET", r"/export$")[0].url.params["mimeType"] == "text/html"


async def test_text_files_are_read_as_media_in_their_charset():
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("alt") == "media":
            return httpx.Response(
                200,
                content="latin é".encode("latin-1"),
                headers={"content-type": "text/plain; charset=iso-8859-1"},
            )
        return httpx.Response(200, json=drive_file("t", "notes.txt", "text/plain"))

    fake = FakeGoogle().on("GET", FILE, answer)
    ex, _ = fake.executor()
    res = await ex.execute(USER, "drive.read", {"file_id": "t"})
    assert res.ok and res.data["text"] == "latin é"
    assert not fake.calls("GET", r"/export$")


async def test_download_caps_size_and_flags_truncation(monkeypatch):
    monkeypatch.setattr("mavis.tools.integrations.native.google.DOWNLOAD_CAP_BYTES", 1000)
    fake = FakeGoogle().on("GET", FILE, httpx.Response(200, json=drive_file("f", "Big")))
    fake.on(
        "GET", r"/export$", httpx.Response(200, content=b"x" * 50_000, headers={"content-type": "text/plain"})
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "drive.download", {"file_id": "f"})
    assert res.ok and res.data["truncated"] and len(res.data["text"]) == 1000


@pytest.mark.parametrize(
    "mime",
    [
        "application/pdf",
        "image/png",
        "application/vnd.google-apps.folder",
        "application/vnd.google-apps.form",
        "application/zip",
    ],
)
async def test_non_text_files_are_refused_without_downloading(mime):
    fake = FakeGoogle().on("GET", FILE, httpx.Response(200, json=drive_file("f", "N", mime)))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "drive.read", {"file_id": "f"})
    assert not res.ok and res.error_kind.value == "invalid_argument"
    assert len(fake.requests) == 1


async def test_export_too_large_is_invalid_argument():
    fake = FakeGoogle().on("GET", FILE, httpx.Response(200, json=drive_file("f", "N")))
    fake.on("GET", r"/export$", error(403, "exportSizeLimitExceeded", "too big"))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "drive.download", {"file_id": "f"})
    assert res.error_kind.value == "invalid_argument"


# --- docs and sheets ----------------------------------------------------------------------------------


async def test_docs_read_returns_the_docs_resource_the_renderer_reads():
    doc = {
        "documentId": "d1",
        "title": "Roadmap",
        "body": {
            "content": [
                {"endIndex": 1, "sectionBreak": {}},
                {"endIndex": 12, "paragraph": {"elements": [{"textRun": {"content": "Q4 plans.\n"}}]}},
            ]
        },
    }
    fake = FakeGoogle().on("GET", r"/v1/documents/d1$", httpx.Response(200, json=doc))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "docs.read", {"document_id": "d1"})
    assert "Roadmap" in wr.render_doc(res.data) and "Q4 plans." in wr.render_doc(res.data)
    assert wr.doc_end_index(res.data) == 11


async def test_sheets_find_filters_to_spreadsheets():
    fake = FakeGoogle().on(
        "GET",
        FILES,
        httpx.Response(
            200, json={"files": [drive_file("s1", "Budget 2026", "application/vnd.google-apps.spreadsheet")]}
        ),
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "sheets.find", {"query": "name contains 'budget'"})
    q = fake.requests[0].url.params["q"]
    assert "mimeType = 'application/vnd.google-apps.spreadsheet'" in q and "name contains 'budget'" in q
    assert "spreadsheet_id=s1 | Budget 2026" in wr.render_sheet_list(res.data)
    await ex.execute(USER, "sheets.find", {})
    assert fake.requests[-1].url.params["q"].startswith("mimeType = ")


async def test_sheets_read_defaults_to_the_first_sheet_and_quotes_its_title():
    fake = FakeGoogle().on(
        "GET",
        r"/v4/spreadsheets/s1$",
        httpx.Response(
            200, json={"sheets": [{"properties": {"title": "Bob's Q3"}}, {"properties": {"title": "Other"}}]}
        ),
    )
    fake.on(
        "GET",
        r"/values/",
        lambda r: httpx.Response(
            200,
            json={
                "range": "'Bob''s Q3'!A1:C3",
                "majorDimension": "ROWS",
                "values": [["Name", "Amt"], ["x", 3]],
            },
        ),
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "sheets.read", {"spreadsheet_id": "s1"})
    assert (
        fake.requests[-1]
        .url.raw_path.decode()
        .endswith("/values/%27Bob%27%27s%20Q3%27?valueRenderOption=FORMATTED_VALUE")
    )
    assert wr.has_value_ranges(res.data)
    where, rows = wr.sheet_rows(res.data)
    assert where == "'Bob''s Q3'!A1:C3" and rows == [["Name", "Amt"], ["x", "3"]]
    assert "Name | Amt" in wr.render_sheet(res.data)


async def test_sheets_read_explicit_range_and_empty_range_still_has_a_value_range():
    fake = FakeGoogle().on(
        "GET", r"/values/", httpx.Response(200, json={"range": "Sheet1!B2:C3", "majorDimension": "ROWS"})
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "sheets.read", {"spreadsheet_id": "s1", "range": "Sheet1!B2:C3"})
    assert len(fake.requests) == 1 and "Sheet1" in fake.requests[0].url.path
    assert wr.has_value_ranges(res.data) and wr.sheet_rows(res.data)[1] == []


async def test_sheets_read_caps_rows():
    rows = [[str(i)] for i in range(1000)]
    fake = FakeGoogle().on(
        "GET", r"/values/", httpx.Response(200, json={"range": "A1:A1000", "values": rows})
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "sheets.read", {"spreadsheet_id": "s", "range": "A1:A1000"})
    assert len(wr.sheet_rows(res.data)[1]) == 200


# --- people -------------------------------------------------------------------------------------------

SEARCH = r"/people:searchContacts$"


async def test_contacts_search_warms_up_once_then_searches_and_renders():
    person = {
        "person": {
            "names": [{"displayName": "Priya Nair"}],
            "emailAddresses": [{"value": "priya@x.com"}],
            "phoneNumbers": [{"value": "+91 99999"}],
        }
    }
    fake = FakeGoogle().on(
        "GET",
        SEARCH,
        lambda r: httpx.Response(200, json={"results": [person]} if r.url.params["query"] else {}),
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "contacts.search", {"query": "priya", "max_results": 5})
    again = await ex.execute(USER, "contacts.search", {"query": "nair"})
    saved = [r for r in fake.requests if r.url.path.endswith("people:searchContacts")]
    queries = [r.url.params["query"] for r in saved]
    assert queries == ["", "priya", "nair"]  # one warm-up, first, per user
    assert saved[1].url.params["pageSize"] == "5"
    assert "Priya Nair | email: priya@x.com | phone: +91 99999" in wr.render_contacts(res.data)
    assert again.ok
    other = await ex.execute(UserRef(user_id=6), "contacts.search", {"query": "priya"})
    later = [r for r in fake.requests if r.url.path.endswith("people:searchContacts")][3:]
    assert other.ok and [r.url.params["query"] for r in later] == ["", "priya"]


async def test_contacts_search_no_match_is_empty():
    fake = FakeGoogle().on("GET", SEARCH, httpx.Response(200, json={}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "contacts.search", {"query": "zzz"})
    assert res.ok and res.data["results"] == [] and "No contacts" in wr.render_contacts(res.data)


async def test_contacts_list_follows_pages_under_connections():
    pages = {
        None: {"connections": [{"emailAddresses": [{"value": "a@x.com"}]}], "nextPageToken": "n"},
        "n": {"connections": [{"emailAddresses": [{"value": "b@x.com"}]}]},
    }
    fake = FakeGoogle().on(
        "GET",
        r"/people/me/connections$",
        lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken")]),
    )
    ex, _ = fake.executor()
    res = await ex.execute(USER, "contacts.list", {})
    people = extract_list(res.data, "connections")
    assert [p["emailAddresses"][0]["value"] for p in people] == ["a@x.com", "b@x.com"]
    assert "emailAddresses" in fake.requests[0].url.params["personFields"]


async def test_create_event_keeps_a_named_zone_wall_clock():
    fake = FakeGoogle().on("POST", EVENTS, httpx.Response(200, json={"id": "new"}))
    ex, _ = fake.executor()
    start = datetime(2026, 10, 6, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    await ex.execute(
        USER, "calendar.create_event", {"summary": "Call", "start": start, "duration_minutes": 30}
    )
    body = body_of(fake.requests[0])
    assert body["start"] == {"dateTime": "2026-10-06T15:00:00", "timeZone": "America/New_York"}
    assert body["end"]["dateTime"] == "2026-10-06T15:30:00"
