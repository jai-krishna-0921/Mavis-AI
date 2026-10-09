"""Workspace writes against fake Drive, Docs, Sheets, Tasks and Meet APIs shaped like the real ones.

Each result is checked through the code that reads it (workspace_render, workspace_guard.created_ids,
attention task polling), and every non-idempotent write is checked to be sent exactly once on a 5xx."""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from mavis.config import get_settings
from mavis.domain.errors import FailureKind
from mavis.domain.integrations import UserRef
from mavis.tools.integrations import workspace_render as wr
from mavis.tools.integrations.normalize import extract_list
from mavis.tools.integrations.workspace_guard import created_ids

from .conftest import *  # noqa: F403 - fixtures
from .gfake import FakeGoogle, body_of, error

USER = UserRef(user_id=7)
FILES = r"/drive/v3/files$"
FILE = r"/drive/v3/files/[^/]+$"
DOC = r"/v1/documents$"
BATCH = r"/v1/documents/[^/]+:batchUpdate$"
TASKS = r"/tasks/v1/lists/@default/tasks$"
TASK = r"/tasks/v1/lists/@default/tasks/[^/]+$"


def ok(payload: dict | None = None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload if payload is not None else {})


async def run(fake: FakeGoogle, action: str, args: dict):
    ex, sleeps = fake.executor()
    return await ex.execute(USER, action, args), sleeps


# --- Drive ----------------------------------------------------------------------------------------------


async def test_create_folder_posts_a_folder_with_its_parent_once():
    fake = FakeGoogle().on("POST", FILES, ok({"id": "F1", "name": "Q4", "mimeType": wr.KINDS and
                                              "application/vnd.google-apps.folder"}))
    res, _ = await run(fake, "drive.create_folder", {"name": "Q4", "parent_id": "P9"})
    assert res.ok and created_ids(res.data) == ["F1"] and wr.render_created(res.data).startswith("Done.")
    req = fake.requests[0]
    assert body_of(req) == {"name": "Q4", "mimeType": "application/vnd.google-apps.folder", "parents": ["P9"]}
    assert req.headers["authorization"] == "Bearer tok"


async def test_create_folder_in_my_drive_sends_no_parents():
    fake = FakeGoogle().on("POST", FILES, ok({"id": "F2", "name": "x"}))
    await run(fake, "drive.create_folder", {"name": "x"})
    assert "parents" not in body_of(fake.requests[0])


async def test_move_with_a_known_source_adds_and_removes_in_one_patch():
    fake = FakeGoogle().on("PATCH", FILE, ok({"id": "f1", "name": "a", "parents": ["B"]}))
    res, _ = await run(fake, "drive.move", {"file_id": "f1", "to_folder_id": "B", "from_folder_id": "A"})
    assert res.ok and len(fake.requests) == 1
    q = fake.requests[0].url.params
    assert q["addParents"] == "B" and q["removeParents"] == "A" and q["supportsAllDrives"] == "true"


async def test_move_without_a_source_removes_every_current_parent_except_the_destination():
    fake = (FakeGoogle().on("GET", FILE, ok({"parents": ["A", "C", "B"]}))
            .on("PATCH", FILE, ok({"id": "f1", "name": "a", "parents": ["B"]})))
    res, _ = await run(fake, "drive.move", {"file_id": "f1", "to_folder_id": "B"})
    assert res.ok
    assert fake.calls("PATCH", FILE)[0].url.params["removeParents"] == "A,C"


async def test_move_is_repeated_after_a_5xx_because_it_sets_a_fixed_state():
    fake = FakeGoogle().on("PATCH", FILE, [error(503, "backendError"), ok({"id": "f1", "name": "a"})])
    res, sleeps = await run(fake, "drive.move", {"file_id": "f1", "to_folder_id": "B", "from_folder_id": "A"})
    assert res.ok and len(fake.calls("PATCH", FILE)) == 2 and sleeps


async def test_share_notifies_by_email_and_reports_the_file_not_the_permission():
    fake = FakeGoogle().on("POST", r"/files/f1/permissions$", ok(
        {"id": "perm9", "type": "user", "role": "writer", "emailAddress": "bo@acme.com"}))
    res, _ = await run(fake, "drive.share", {"file_id": "f1", "email": "Bo@acme.com", "role": "writer"})
    assert res.ok and res.data["id"] == "f1" and res.data["permissionId"] == "perm9"
    req = fake.requests[0]
    assert req.url.params["sendNotificationEmail"] == "true"
    assert body_of(req) == {"type": "user", "role": "writer", "emailAddress": "Bo@acme.com"}


async def test_comment_sets_the_required_fields_parameter():
    fake = FakeGoogle().on("POST", r"/files/f1/comments$", ok({"id": "c1", "content": "hi"}))
    res, _ = await run(fake, "docs.comment", {"file_id": "f1", "content": "hi"})
    assert res.ok and created_ids(res.data) == ["c1"]
    assert fake.requests[0].url.params["fields"] and body_of(fake.requests[0]) == {"content": "hi"}


@pytest.fixture
def artifacts(settings):
    root = settings.artifacts_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def values_target(request: httpx.Request) -> str:
    """The A1 target after /values/ as Google decodes it."""
    return unquote(request.url.raw_path.decode().split("/values/")[1].split("?")[0])


def multipart_parts(request: httpx.Request) -> tuple[dict, bytes, str]:
    ctype = request.headers["content-type"]
    assert ctype.startswith("multipart/related; boundary=")
    boundary = ctype.split("boundary=")[1].encode()
    pieces = request.content.split(b"--" + boundary)
    meta = pieces[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
    head, media = pieces[2].split(b"\r\n\r\n", 1)
    return json.loads(meta), media.rsplit(b"\r\n", 1)[0], head.decode()


async def test_upload_sends_one_multipart_request_with_metadata_and_bytes(artifacts):
    path = artifacts / "report.csv"
    path.write_bytes(b"a,b\n1,2\n")
    fake = FakeGoogle().on("POST", r"/upload/drive/v3/files$", ok({"id": "U1", "name": "report.csv"}))
    res, _ = await run(fake, "drive.upload_file", {"path": str(path), "name": "report.csv",
                                                   "mime": "text/csv", "folder_id": "P1"})
    assert res.ok and created_ids(res.data) == ["U1"]
    req = fake.requests[0]
    assert req.url.params["uploadType"] == "multipart"
    meta, media, head = multipart_parts(req)
    assert meta == {"name": "report.csv", "mimeType": "text/csv", "parents": ["P1"]}
    assert media == b"a,b\n1,2\n" and "Content-Type: text/csv" in head


async def test_upload_with_a_hostile_mime_falls_back_to_octet_stream(artifacts):
    path = artifacts / "x.bin"
    path.write_bytes(b"1")
    fake = FakeGoogle().on("POST", r"/upload/drive/v3/files$", ok({"id": "U"}))
    await run(fake, "drive.upload_file", {"path": str(path), "name": "x", "mime": "a/b\r\nX: y"})
    assert multipart_parts(fake.requests[0])[0]["mimeType"] == "application/octet-stream"


@pytest.mark.parametrize("where", ["outside", "traversal", "missing", "symlink"])
async def test_upload_reads_only_from_the_artifact_directory(artifacts, tmp_path, where):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    target = {
        "outside": secret, "traversal": artifacts / ".." / ".." / "secret.txt",
        "missing": artifacts / "nope.txt", "symlink": artifacts / "link.txt",
    }[where]
    if where == "symlink":
        target.symlink_to(secret)
    fake = FakeGoogle().on("POST", r"/upload/drive/v3/files$", ok({"id": "U"}))
    res, _ = await run(fake, "drive.upload_file", {"path": str(target), "name": "n", "mime": "text/plain"})
    assert not res.ok and res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field == "path"
    assert fake.requests == []


async def test_upload_over_the_size_cap_is_refused_not_truncated(artifacts, monkeypatch):
    from mavis.tools.integrations.native import google
    monkeypatch.setattr(google, "UPLOAD_CAP_BYTES", 10)
    path = artifacts / "big.bin"
    path.write_bytes(b"x" * 11)
    fake = FakeGoogle().on("POST", r"/upload/drive/v3/files$", ok({"id": "U"}))
    res, _ = await run(fake, "drive.upload_file", {"path": str(path), "name": "big", "mime": "text/plain"})
    assert not res.ok and "5 MB" in res.error and fake.requests == []
    path.write_bytes(b"x" * 10)
    again, _ = await run(fake, "drive.upload_file", {"path": str(path), "name": "ok", "mime": "text/plain"})
    assert again.ok


# --- Docs -----------------------------------------------------------------------------------------------


async def test_docs_create_makes_the_doc_then_writes_the_converted_markdown_once():
    fake = (FakeGoogle().on("POST", DOC, ok({"documentId": "D1", "title": "Plan"}))
            .on("POST", BATCH, ok({"documentId": "D1", "replies": [{}]})))
    res, _ = await run(fake, "docs.create", {"title": "Plan", "markdown": "# Goals\n- **ship** it"})
    assert res.ok and created_ids(res.data) == ["D1"]
    assert wr.render_created(res.data) == 'Done. {"documentId": "D1", "title": "Plan"}'
    assert body_of(fake.requests[0]) == {"title": "Plan"}
    batch = fake.calls("POST", BATCH)
    assert len(batch) == 1 and batch[0].url.path.endswith("/D1:batchUpdate")
    reqs = body_of(batch[0])["requests"]
    assert reqs[0]["insertText"]["text"] == "Goals\nship it"
    kinds = [next(iter(r)) for r in reqs]
    assert "updateParagraphStyle" in kinds and "updateTextStyle" in kinds
    assert kinds[-1] == "createParagraphBullets"


async def test_docs_create_with_no_body_skips_the_second_call():
    fake = FakeGoogle().on("POST", DOC, ok({"documentId": "D2", "title": "Empty"}))
    res, _ = await run(fake, "docs.create", {"title": "Empty"})
    assert res.ok and len(fake.requests) == 1


async def test_docs_create_trashes_the_empty_doc_when_the_body_is_refused():
    fake = (FakeGoogle().on("POST", DOC, ok({"documentId": "D3", "title": "T"}))
            .on("POST", BATCH, error(400, "invalid", "bad range"))
            .on("PATCH", FILE, ok({"id": "D3"})))
    res, _ = await run(fake, "docs.create", {"title": "T", "markdown": "text"})
    assert not res.ok and res.error_kind is FailureKind.INVALID_ARGUMENT
    trash = fake.calls("PATCH", FILE)
    assert len(trash) == 1 and body_of(trash[0]) == {"trashed": True} and trash[0].url.path.endswith("/D3")


async def test_docs_create_keeps_the_doc_and_names_it_when_the_body_outcome_is_unknown():
    fake = (FakeGoogle().on("POST", DOC, ok({"documentId": "D4", "title": "T"}))
            .on("POST", BATCH, error(503, "backendError")))
    res, _ = await run(fake, "docs.create", {"title": "T", "markdown": "text"})
    assert res.error_kind is FailureKind.UNCONFIRMED and "D4" in res.error
    assert len(fake.calls("POST", BATCH)) == 1 and fake.calls("PATCH", FILE) == []


async def test_docs_create_refuses_a_huge_body_before_creating_anything():
    fake = FakeGoogle().on("POST", DOC, ok({"documentId": "D5"}))
    res, _ = await run(fake, "docs.create", {"title": "T", "markdown": "x" * 100_001})
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field == "markdown"
    assert fake.requests == []


async def test_insert_text_is_the_batch_update_docs_append_relies_on():
    fake = FakeGoogle().on("POST", BATCH, ok({"documentId": "D1", "replies": [{}]}))
    res, _ = await run(fake, "docs.insert_text", {"document_id": "D1", "text": "\nmore", "index": 41})
    assert res.ok
    assert body_of(fake.requests[0]) == {"requests": [{"insertText": {"location": {"index": 41},
                                                                      "text": "\nmore"}}]}


async def test_append_flow_reads_end_index_from_the_docs_read_shape():
    doc = {"documentId": "D1", "title": "T", "body": {"content": [
        {"endIndex": 1, "sectionBreak": {}},
        {"startIndex": 1, "endIndex": 12,
         "paragraph": {"elements": [{"textRun": {"content": "hello world\n"}}]}}]}}
    fake = FakeGoogle().on("GET", r"/v1/documents/D1$", ok(doc))
    res, _ = await run(fake, "docs.read", {"document_id": "D1"})
    assert wr.doc_end_index(res.data) == 11  # just before the final newline: where append inserts


# --- Sheets ---------------------------------------------------------------------------------------------


async def test_sheets_create_returns_the_spreadsheet_id_and_title():
    fake = FakeGoogle().on("POST", r"/v4/spreadsheets$", ok(
        {"spreadsheetId": "S1", "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/S1/edit",
         "properties": {"title": "Budget"}}))
    res, _ = await run(fake, "sheets.create", {"title": "Budget"})
    assert res.ok and created_ids(res.data) == ["S1"]
    assert wr.render_created(res.data) == 'Done. {"spreadsheetId": "S1", "title": "Budget"}'
    assert body_of(fake.requests[0]) == {"properties": {"title": "Budget"}}


APPEND = r"/v4/spreadsheets/S1/values/.+:append$"


async def test_append_row_uses_user_entered_inserted_rows_and_quotes_the_sheet():
    fake = FakeGoogle().on("POST", APPEND, ok(
        {"spreadsheetId": "S1", "tableRange": "Sheet1!A1:B2", "updates": {"updatedRows": 1}}))
    res, _ = await run(fake, "sheets.append_row", {"spreadsheet_id": "S1", "range": "Sheet1",
                                                    "values": ["lunch", 12.5, True]})
    assert res.ok and res.data["spreadsheetId"] == "S1"
    req = fake.requests[0]
    assert values_target(req) == "'Sheet1':append"
    q = req.url.params
    assert q["valueInputOption"] == "USER_ENTERED" and q["insertDataOption"] == "INSERT_ROWS"
    assert body_of(req) == {"majorDimension": "ROWS", "values": [["lunch", 12.5, True]]}


@pytest.mark.parametrize(("given", "sent"), [
    ("My Budget", "'My Budget'"), ("O'Neil", "'O''Neil'"), ("Tab!A1:B2", "Tab!A1:B2"),
    ("'Q1 Data'", "'Q1 Data'"), ("A1:D5", "A1:D5"),
])
async def test_append_row_range_forms(given, sent):
    fake = FakeGoogle().on("POST", APPEND, ok({"spreadsheetId": "S1"}))
    await run(fake, "sheets.append_row", {"spreadsheet_id": "S1", "range": given, "values": ["x"]})
    assert values_target(fake.requests[0]) == f"{sent}:append"


@pytest.mark.parametrize("cell", ["=IMAGE(\"http://evil\")", "+1", "-2+3", "@SUM(A1)"])
async def test_a_formula_like_cell_is_written_raw_never_evaluated(cell):
    fake = FakeGoogle().on("POST", APPEND, ok({"spreadsheetId": "S1"}))
    await run(fake, "sheets.append_row", {"spreadsheet_id": "S1", "values": ["ok", cell]})
    assert fake.requests[0].url.params["valueInputOption"] == "RAW"


async def test_update_range_puts_the_block_at_the_start_cell():
    fake = FakeGoogle().on("PUT", r"/v4/spreadsheets/S1/values/.+$", ok(
        {"spreadsheetId": "S1", "updatedRange": "Data!B2:C3", "updatedCells": 4}))
    res, _ = await run(fake, "sheets.update_range", {
        "spreadsheet_id": "S1", "sheet_name": "Data", "start_cell": "b2", "values": [[1, 2], ["a", "b"]]})
    assert res.ok and res.data["updatedCells"] == 4
    req = fake.requests[0]
    assert values_target(req) == "'Data'!B2"
    assert req.url.params["valueInputOption"] == "USER_ENTERED"
    assert body_of(req) == {"majorDimension": "ROWS", "values": [[1, 2], ["a", "b"]]}


async def test_update_range_is_repeated_after_a_5xx_because_it_writes_fixed_values():
    fake = FakeGoogle().on("PUT", r"/values/", [error(500), ok({"spreadsheetId": "S1"})])
    res, _ = await run(fake, "sheets.update_range", {
        "spreadsheet_id": "S1", "sheet_name": "D", "start_cell": "A1", "values": [[1]]})
    assert res.ok and len(fake.requests) == 2


# --- Tasks ----------------------------------------------------------------------------------------------


def task(tid: str, title: str = "Pay rent", **kw) -> dict:
    return {"kind": "tasks#task", "id": tid, "title": title, "status": "needsAction",
            "updated": "2026-10-08T10:00:00.000Z", **kw}


async def test_tasks_list_filters_and_uses_the_keys_both_readers_expect():
    fake = FakeGoogle().on("GET", TASKS, ok({"kind": "tasks#tasks", "items": [
        task("t1", due="2026-10-09T00:00:00.000Z", notes="n"), task("t2", status="completed")]}))
    res, _ = await run(fake, "tasks.list", {"due_before": "2026-10-09T23:59:59+00:00", "max_results": 100})
    assert res.ok
    q = fake.requests[0].url.params
    assert q["dueMax"] == "2026-10-09T23:59:59+00:00" and q["showCompleted"] == "false"
    assert q["maxResults"] == "100"
    polled = extract_list(res.data, "tasks", "data.tasks")  # the key attention polling reads
    assert [t["id"] for t in polled] == ["t1", "t2"]
    text = wr.render_tasks(res.data)
    assert "task_id=t1 | Pay rent | due 2026-10-09 | notes: n" in text and "(done)" in text


async def test_tasks_list_follows_pages_up_to_the_limit():
    pages = {None: {"items": [task("a"), task("b")], "nextPageToken": "N"}, "N": {"items": [task("c")]}}
    fake = FakeGoogle().on("GET", TASKS, lambda r: ok(pages[r.url.params.get("pageToken")]))
    res, _ = await run(fake, "tasks.list", {"max_results": 3, "show_completed": True})
    assert [t["id"] for t in res.data["tasks"]] == ["a", "b", "c"]
    assert fake.requests[0].url.params["showCompleted"] == "true"


async def test_tasks_get_returns_the_raw_task_the_guards_read():
    fake = FakeGoogle().on("GET", TASK, ok(task("t1", deleted=True)))
    res, _ = await run(fake, "tasks.get", {"task_id": "t1"})
    assert res.data["title"] == "Pay rent" and res.data["deleted"] is True


async def test_tasks_add_sends_date_only_due_and_returns_the_new_id():
    fake = FakeGoogle().on("POST", TASKS, ok(task("t9", "Call Bo", due="2026-10-12T00:00:00.000Z")))
    res, _ = await run(fake, "tasks.add", {"title": "Call Bo", "notes": "re: invoice", "due": "2026-10-12"})
    assert res.ok and created_ids(res.data) == ["t9"]
    assert body_of(fake.requests[0]) == {"title": "Call Bo", "status": "needsAction", "notes": "re: invoice",
                                         "due": "2026-10-12T00:00:00.000Z"}


async def test_tasks_patch_completes_a_task():
    fake = FakeGoogle().on("PATCH", TASK, ok(task("t1", status="completed")))
    res, _ = await run(fake, "tasks.patch", {"task_id": "t1", "title": "Pay rent", "status": "completed"})
    assert res.ok and body_of(fake.requests[0]) == {"title": "Pay rent", "status": "completed"}


async def test_tasks_patch_reopening_clears_the_completion_time():
    fake = FakeGoogle().on("PATCH", TASK, ok(task("t1")))
    await run(fake, "tasks.patch", {"task_id": "t1", "title": "Pay rent", "status": "needsAction",
                                    "notes": "x", "due": date(2026, 10, 20)})
    assert body_of(fake.requests[0]) == {"title": "Pay rent", "status": "needsAction", "completed": None,
                                         "notes": "x", "due": "2026-10-20T00:00:00.000Z"}


async def test_tasks_delete_handles_the_empty_204_answer():
    fake = FakeGoogle().on("DELETE", TASK, httpx.Response(204))
    res, _ = await run(fake, "tasks.delete", {"task_id": "t1"})
    assert res.ok and wr.render_created(res.data) == 'Done. {"id": "t1"}'


async def test_tasks_delete_of_a_missing_task_is_not_found():
    fake = FakeGoogle().on("DELETE", TASK, error(404, "notFound", "Not Found"))
    res, _ = await run(fake, "tasks.delete", {"task_id": "gone"})
    assert res.error_kind is FailureKind.NOT_FOUND and res.error_field == "task_id"


async def test_a_task_id_cannot_address_another_endpoint():
    fake = FakeGoogle().on("GET", r".", ok({}))
    res, _ = await run(fake, "tasks.get", {"task_id": "../x"})
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and fake.requests == []


# --- Meet -----------------------------------------------------------------------------------------------


async def test_meet_create_returns_the_link_the_renderer_accepts():
    fake = FakeGoogle().on("POST", r"/v2/spaces$", ok({
        "name": "spaces/abc", "meetingUri": "https://meet.google.com/abc-defg-hij",
        "meetingCode": "abc-defg-hij", "config": {"accessType": "TRUSTED"}}))
    res, _ = await run(fake, "meet.create", {})
    assert res.ok and "https://meet.google.com/abc-defg-hij" in str(wr.render_meet(res.data))
    assert fake.requests[0].url.host == "meet.googleapis.com"


async def test_meet_transcripts_list_for_a_conference_record():
    fake = FakeGoogle().on("GET", r"/v2/conferenceRecords/abc-123/transcripts$", ok({"transcripts": [{
        "name": "conferenceRecords/abc-123/transcripts/t1", "state": "FILE_GENERATED",
        "docsDestination": {"document": "DOC77", "exportUri": "https://docs.google.com/document/d/DOC77"}}]}))
    res, _ = await run(fake, "meet.transcript", {"conference_record_id": "abc-123"})
    assert res.ok
    rendered = wr.render_transcripts(res.data)
    assert "document_id=DOC77" in rendered and "FILE_GENERATED" in rendered
    assert parse_qs(fake.requests[0].url.query.decode())["pageSize"] == ["100"]


async def test_meet_transcript_for_an_unknown_record_is_not_found():
    fake = FakeGoogle().on("GET", r"/transcripts$", error(404, "notFound", "no such record"))
    res, _ = await run(fake, "meet.transcript", {"conference_record_id": "zzz"})
    assert res.error_kind is FailureKind.NOT_FOUND and res.error_field == "conference_record_id"


# --- scope missing from Google itself, and retry rules for every non-idempotent write -------------------


UPLOAD_ARGS = {"name": "n", "mime": "text/plain"}
NON_IDEMPOTENT = [
    ("drive.create_folder", {"name": "F"}, "POST", FILES),
    ("drive.share", {"file_id": "f1", "email": "a@x.com"}, "POST", r"/permissions$"),
    ("drive.upload_file", None, "POST", r"/upload/drive/v3/files$"),
    ("docs.create", {"title": "T"}, "POST", DOC),
    ("docs.insert_text", {"document_id": "D", "text": "x", "index": 1}, "POST", BATCH),
    ("docs.comment", {"file_id": "f1", "content": "c"}, "POST", r"/comments$"),
    ("sheets.create", {"title": "S"}, "POST", r"/v4/spreadsheets$"),
    ("sheets.append_row", {"spreadsheet_id": "S1", "values": ["a"]}, "POST", APPEND),
    ("tasks.add", {"title": "t"}, "POST", TASKS),
    ("meet.create", {}, "POST", r"/v2/spaces$"),
]


WRITE_CASES = pytest.mark.parametrize(
    ("action", "args", "method", "route"), NON_IDEMPOTENT, ids=[w[0] for w in NON_IDEMPOTENT])


def with_file(args: dict | None, artifacts) -> dict:
    if args is not None:
        return args
    path = artifacts / "f.txt"
    path.write_text("hi")
    return {"path": str(path), **UPLOAD_ARGS}


@WRITE_CASES
@pytest.mark.parametrize("failure", ["5xx", "read-error"])
async def test_a_non_idempotent_write_is_sent_once_and_reported_unconfirmed(
    artifacts, action, args, method, route, failure
):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    answer = error(503, "backendError") if failure == "5xx" else boom
    fake = FakeGoogle().on(method, route, answer)
    res, sleeps = await run(fake, action, with_file(args, artifacts))
    assert len(fake.calls(method, route)) == 1 and sleeps == []
    assert not res.ok and res.error_kind is FailureKind.UNCONFIRMED


@WRITE_CASES
async def test_a_write_that_never_left_the_machine_may_be_sent_again(artifacts, action, args, method, route):
    attempts = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("down")
        return ok({"documentId": "D", "id": "x", "spreadsheetId": "S", "name": "n"})

    fake = FakeGoogle().on(method, route, flaky)
    res, _ = await run(fake, action, with_file(args, artifacts))
    assert res.ok and len(attempts) == 2


async def test_google_scope_error_on_a_write_is_an_auth_failure_not_a_crash():
    fake = FakeGoogle().on("POST", TASKS, httpx.Response(403, json={"error": {
        "code": 403, "message": "Request had insufficient authentication scopes.",
        "status": "PERMISSION_DENIED",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}))
    res, _ = await run(fake, "tasks.add", {"title": "t"})
    assert not res.ok and res.error_kind is FailureKind.AUTH and len(fake.requests) == 1


async def test_a_revoked_grant_on_a_write_asks_to_reconnect():
    from .gfake import Tokens
    fake = FakeGoogle().on("POST", TASKS, ok(task("t")))
    ex, _ = fake.executor(Tokens(revoked=True))
    res = await ex.execute(USER, "tasks.add", {"title": "t"})
    assert res.error_kind is FailureKind.AUTH and fake.requests == []


async def test_invalid_arguments_never_reach_google():
    fake = FakeGoogle().on("POST", r".", ok({}))
    ex, _ = fake.executor()
    for action, args in [("drive.share", {"file_id": "f", "email": "not-an-email"}),
                         ("sheets.update_range", {"spreadsheet_id": "S", "sheet_name": "s",
                                                  "start_cell": "11A", "values": [[1]]}),
                         ("tasks.add", {"title": ""})]:
        res = await ex.execute(USER, action, args)
        assert res.error_kind is FailureKind.INVALID_ARGUMENT
    assert fake.requests == []


def test_artifact_directory_setting_is_what_uploads_are_checked_against(settings):
    assert get_settings().artifacts_dir.name == "artifacts"
