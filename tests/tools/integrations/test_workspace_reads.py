"""Workspace read actions: argument translation (golden dicts), renderers, drive.read and registration."""

from __future__ import annotations

from datetime import UTC, datetime

import httpcore
import pytest

from mavis.domain.errors import ActionFailed
from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import Capability
from mavis.tools.integrations import actions as a
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS, slug_for
from mavis.tools.integrations.tools import register_integration_tools
from mavis.tools.integrations.workspace_render import (
    render_contacts,
    render_doc,
    render_files,
    render_sheet,
    render_tasks,
    render_transcripts,
)
from mavis.tools.integrations.workspace_tools import drive_read
from mavis.tools.registry import ToolContext, ToolRegistry

CTX = ToolContext(user_id=1, timezone="Asia/Kolkata")


def tr(action: str, args) -> dict:
    return COMPOSIO_ACTIONS[action].translate(args)


def test_read_translations_golden():
    assert tr("drive.search", a.DriveSearchArgs(query="name contains 'deck'", max_results=5)) == {
        "q": "(name contains 'deck') and trashed = false", "pageSize": 5,
        "fields": COMPOSIO_ACTIONS["drive.search"].translate(a.DriveSearchArgs())["fields"],
        "orderBy": "modifiedTime desc",
    }
    assert "orderBy" not in tr("drive.search", a.DriveSearchArgs(query="fullText contains 'Priya'"))
    shared = tr("drive.list_recent", a.DriveRecentArgs(shared_with_me=True))
    assert shared["q"] == "sharedWithMe and trashed = false" and shared["orderBy"] == "sharedWithMeTime desc"
    assert tr("docs.read", a.DocArgs(document_id="d1")) == {"id": "d1"}
    assert tr("sheets.find", a.SheetsFindArgs()) == {"max_results": 10}
    assert tr("sheets.read", a.SheetsReadArgs(spreadsheet_id="s1", range="Sheet1!A1:C9")) == {
        "spreadsheet_id": "s1", "ranges": ["Sheet1!A1:C9"],
    }
    due = datetime(2026, 10, 3, 18, 29, 59, tzinfo=UTC)
    assert tr("tasks.list", a.TasksListArgs(due_before=due)) == {
        "tasklist_id": "@default", "showCompleted": False, "maxResults": 50, "dueMax": "2026-10-03T18:29:59Z",
    }
    assert tr("contacts.search", a.ContactsSearchArgs(query="Priya")) == {"query": "Priya", "pageSize": 10}
    assert tr("meet.transcript", a.MeetTranscriptArgs(conference_record_id="cr1")) == {
        "conferenceRecord_id": "cr1",
    }
    assert tr("drive.meta", a.FileArgs(file_id="f1")) == {"fileId": "f1"}
    assert tr("drive.permissions", a.FileArgs(file_id="f1")) == {"fileId": "f1"}
    assert tr("drive.download", a.DriveDownloadArgs(file_id="f1")) == {"file_id": "f1"}
    assert tr("tasks.get", a.TaskRefArgs(task_id="t1")) == {"tasklist_id": "@default", "task_id": "t1"}
    assert slug_for("mail.profile", "googlesuper") == "GOOGLESUPER_GET_PROFILE"
    assert slug_for("drive.search", "googlesuper") == "GOOGLESUPER_FIND_FILE"
    assert "drive.read" not in COMPOSIO_ACTIONS  # served by workspace_tools.drive_read


def test_render_files_has_ids_owners_and_no_content():
    out = render_files({"files": [
        {"id": "f1", "name": "Q3 deck https://evil.example/x",
         "mimeType": "application/vnd.google-apps.presentation", "modifiedTime": "2026-10-02T10:00:00Z",
         "owners": [{"displayName": "Priya", "emailAddress": "p@x.com"}],
         "sharedWithMeTime": "2026-10-02T11:00:00Z", "sharingUser": {"displayName": "Priya"}},
        {"id": "f2", "name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet",
         "owners": [{"me": True}]},
    ]})
    assert "file_id=f1 | Q3 deck [link] | Slides | owner: Priya | modified: 2026-10-02" in out
    assert "shared with you 2026-10-02 by Priya" in out
    assert "file_id=f2 | Budget | Sheet | owner: you" in out
    assert "evil.example" not in out
    assert render_files({"files": []}) == "No Drive files matched."


def test_render_doc_extracts_text_strips_urls_and_caps():
    doc = {"response_data": {"documentId": "d1", "title": "Notes", "body": {"content": [
        {"endIndex": 1},
        {"endIndex": 30,
         "paragraph": {"elements": [{"textRun": {"content": "Visit https://x.example now\n"}}]}},
        {"endIndex": 9000, "paragraph": {"elements": [{"textRun": {"content": "y" * 8000}}]}},
    ]}}}
    out = render_doc(doc)
    assert out.startswith("document_id=d1 | Notes\n\nVisit [link] now")
    assert out.endswith("...[truncated]") and len(out) < 5600


def test_render_sheet_caps_rows_columns_and_cells():
    values = [[f"r{r}c{c}" for c in range(25)] for r in range(60)]
    values[0][0] = "z" * 200
    out = render_sheet({"spreadsheet_data": {"valueRanges": [{"range": "Sheet1!A1:Y60", "values": values}]}})
    lines = out.splitlines()
    assert lines[0] == "range=Sheet1!A1:Y60 | rows 1-50 of 60 | first 20 columns"
    assert len(lines) == 51
    assert lines[1].split(" | ")[0] == "z" * 80 + "..."
    assert len(lines[2].split(" | ")) == 20
    assert render_sheet({"spreadsheet_data": {"valueRanges": []}}) == "That range is empty."


def test_render_tasks_contacts_transcripts():
    tasks = render_tasks({"tasks": [{"id": "t1", "title": "Pay rent", "due": "2026-10-03T00:00:00.000Z",
                                     "status": "needsAction", "notes": "via bank"}]})
    assert "task_id=t1 | Pay rent | due 2026-10-03 | notes: via bank" in tasks
    people = render_contacts({"response_data": {"results": [{"person": {
        "names": [{"displayName": "Priya Raman"}], "emailAddresses": [{"value": "priya@example.com"}],
        "phoneNumbers": [{"value": "+91 98450 00000"}]}}]}})
    assert people == "- Priya Raman | email: priya@example.com | phone: +91 98450 00000"
    out = render_transcripts({"response_data": {"transcripts": [
        {"name": "conferenceRecords/cr1/transcripts/t1", "state": "FILE_GENERATED",
         "docsDestination": {"document": "doc9"}}]}})
    assert "document_id=doc9" in out


@pytest.fixture
def google(provider):
    provider.set_state(1, Capability.DRIVE, ConnectionState.ACTIVE)
    return provider


async def test_drive_read_exports_a_sheet_as_csv(google, cache):
    google.results["drive.meta"] = ToolResult(ok=True, data={
        "name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"})
    google.results["drive.download"] = ToolResult(ok=True, data={
        "downloaded_file_content": {"name": "Budget.csv", "mimetype": "text/csv", "s3url": "https://s3.example/f"}})
    fetched: list[str] = []

    async def fetch(url: str) -> str:
        fetched.append(url)
        return "month,amount\nOct,1200\n"

    out = await drive_read(CTX, a.FileArgs(file_id="f1"), provider=google, cache=cache, fetch=fetch)
    assert out == "file_id=f1 | Budget | Sheet\n\nmonth,amount\nOct,1200"
    assert google.executed[-1] == (1, "drive.download", {"file_id": "f1", "mime_type": "text/csv"})
    assert fetched == ["https://s3.example/f"]


async def test_drive_read_declines_binary_files(google, cache):
    google.results["drive.meta"] = ToolResult(ok=True, data={"name": "Scan", "mimeType": "application/pdf"})
    out = await drive_read(CTX, a.FileArgs(file_id="f1"), provider=google, cache=cache)
    assert "PDF file, so I can't read its text" in out
    assert [e[1] for e in google.executed] == ["drive.meta"]


async def test_drive_read_fetch_failure_is_action_failed(google, cache):
    google.results["drive.meta"] = ToolResult(ok=True, data={"name": "N", "mimeType": "text/plain"})
    google.results["drive.download"] = ToolResult(ok=True, data={
        "downloaded_file_content": {"s3url": "http://plain.example/f"}})

    async def fetch(url: str) -> str:
        raise ValueError("only https download links are fetched")

    with pytest.raises(ActionFailed, match="could not fetch the file"):
        await drive_read(CTX, a.FileArgs(file_id="f1"), provider=google, cache=cache, fetch=fetch)


def test_workspace_tools_register_only_when_enabled(workspace_on):
    registry = ToolRegistry()
    names = register_integration_tools(registry)
    for name in ("drive_search", "drive_read", "docs_read", "sheets_read", "tasks_list", "contacts_search"):
        assert name in names and registry.get(name).untrusted_output is True
        assert "conversation" in registry.get(name).agents and "spawn" in registry.get(name).agents
    assert "drive_meta" not in names and "mail_profile" not in names


def test_to_do_list_question_offers_tasks_list(workspace_on):
    registry = ToolRegistry()
    register_integration_tools(registry)
    chosen = [t.name for t in registry.select("conversation", 1, "what's on my to-do list", limit=6)]
    assert chosen[0] == "tasks_list"


@pytest.mark.parametrize("exc", [httpcore.ConnectError("x"), httpcore.ReadTimeout("x"),
                                 httpcore.RemoteProtocolError("x")])
async def test_drive_read_httpcore_errors_are_action_failed(google, cache, exc):
    google.results["drive.meta"] = ToolResult(ok=True, data={"name": "N", "mimeType": "text/plain"})
    google.results["drive.download"] = ToolResult(ok=True, data={
        "downloaded_file_content": {"s3url": "https://s3.example/f"}})

    async def fetch(url: str) -> str:
        raise exc

    with pytest.raises(ActionFailed, match="could not fetch the file"):
        await drive_read(CTX, a.FileArgs(file_id="f1"), provider=google, cache=cache, fetch=fetch)


async def test_drive_read_uses_inline_text_without_fetching(google, cache):
    """The native provider exports the text itself: no download link, nothing fetched."""
    google.results["drive.meta"] = ToolResult(ok=True, data={
        "name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"})
    google.results["drive.download"] = ToolResult(ok=True, data={
        "file_id": "f1", "name": "Budget", "mimeType": "text/csv", "text": "month,amount\nOct,1200\n",
        "truncated": False})

    async def fetch(url: str) -> str:
        raise AssertionError("nothing to fetch")

    out = await drive_read(CTX, a.FileArgs(file_id="f1"), provider=google, cache=cache, fetch=fetch)
    assert out == "file_id=f1 | Budget | Sheet\n\nmonth,amount\nOct,1200"
