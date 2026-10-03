"""Spec 4.3: in a tainted task a file-changing or sharing action may only target files (or tasks) the user
named in an untainted root goal, or ones the task created. Anything else is refused before approval."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import RiskClass
from mavis.domain.tasks import TaskOrigin
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks
from mavis.tools.integrations import actions as a
from mavis.tools.integrations import tools as tools_mod
from mavis.tools.integrations import workspace_guard
from mavis.tools.integrations.actions import GOOGLE_CAPABILITIES
from mavis.tools.integrations.tools import register_integration_tools
from mavis.tools.integrations.workspace_guard import REFUSAL, ids_in
from mavis.tools.integrations.workspace_tools import PREPARES
from mavis.tools.registry import ToolContext, ToolRegistry, ToolRun, current_run, current_task_id

PAYROLL = "1PayrollFileIdAbcdefghijklmnop"
DECK = "1DeckFileIdAbcdefghijklmnopqrs"


@pytest.fixture
def google(provider, cache, monkeypatch, user):
    for c in GOOGLE_CAPABILITIES:
        provider.set_state(user.id, c, ConnectionState.ACTIVE)
    monkeypatch.setattr(tools_mod, "_deps", lambda p, c: (provider, cache))
    provider.results["drive.permissions"] = ToolResult(ok=True, data={"permissions": [
        {"id": "p1", "type": "user", "role": "owner"}]})
    provider.results["mail.profile"] = ToolResult(ok=True, data={
        "response_data": {"emailAddress": "j@x.com"}})
    workspace_guard._created.clear()
    return provider


def named(provider, title: str) -> None:
    provider.results["drive.meta"] = ToolResult(ok=True, data={"name": title, "mimeType": "x"})


async def run_prepare(action: str, args, task_id: int, user_id: int, tainted: bool = True):
    token = current_task_id.set(task_id)
    run_token = current_run.set(ToolRun(tainted=tainted))
    try:
        return await PREPARES[action](ToolContext(user_id=user_id, task_id=task_id), args)
    finally:
        current_run.reset(run_token)
        current_task_id.reset(token)


def share(file_id: str) -> a.DriveShareArgs:
    return a.DriveShareArgs(file_id=file_id, email="attacker@evil.example")


async def test_tainted_task_refuses_a_file_the_user_never_named(google, user):
    tid = await tasks.create(user.id, goal="summarize the Q3 deck for me")
    named(google, "Payroll 2026")
    assert (await run_prepare("drive.share", share(PAYROLL), tid, user.id)).refusal == REFUSAL


async def test_title_named_in_the_goal_is_allowed(google, user):
    tid = await tasks.create(user.id, goal="share the Q3 deck with Priya")
    named(google, "Q3 Deck")
    assert (await run_prepare("drive.share", share(DECK), tid, user.id)).refusal is None


async def test_file_id_in_the_goal_is_allowed(google, user):
    tid = await tasks.create(user.id, goal=f"comment on https://docs.google.com/document/d/{DECK}/edit")
    args = a.DocCommentArgs(file_id=DECK, content="ok")
    assert (await run_prepare("docs.comment", args, tid, user.id)).refusal is None


async def test_files_the_task_or_its_parent_created_are_allowed(google, user):
    parent = await tasks.create(user.id, goal="make a budget sheet")
    child = await tasks.create(user.id, goal="fill it in", parent_id=parent)
    workspace_guard.record_created(parent, ["sheet-made-by-parent"])
    args = a.SheetAppendArgs(spreadsheet_id="sheet-made-by-parent", values=["x"])
    assert (await run_prepare("sheets.append_row", args, child, user.id)).refusal is None


async def test_initiative_goal_is_never_an_allowlist_source(google, user):
    tid = await tasks.create(user.id, goal="share the Q3 deck", origin=TaskOrigin.INITIATIVE, tainted=True)
    named(google, "Q3 Deck")
    assert (await run_prepare("drive.share", share(DECK), tid, user.id)).refusal == REFUSAL


async def test_tainted_root_goal_is_not_trusted(google, user):
    tid = await tasks.create(user.id, goal=f"share {DECK}", tainted=True)
    assert (await run_prepare("drive.share", share(DECK), tid, user.id)).refusal == REFUSAL


async def test_lookup_failure_refuses(google, user):
    tid = await tasks.create(user.id, goal="share the Q3 deck with Priya")
    google.results["drive.meta"] = ToolResult(ok=False, error="Composio answered 404 for POST /x")
    assert (await run_prepare("drive.share", share(DECK), tid, user.id)).refusal == REFUSAL


async def test_task_titles_guard_tasks_delete(google, user):
    tid = await tasks.create(user.id, goal="clean up the 'Buy milk' task")
    google.results["tasks.get"] = ToolResult(ok=True, data={"id": "t1", "title": "Buy milk"})
    assert (await run_prepare("tasks.delete", a.TaskDeleteArgs(task_id="t1"), tid, user.id)).refusal is None
    google.results["tasks.get"] = ToolResult(ok=True, data={"id": "t2", "title": "File taxes"})
    out = await run_prepare("tasks.delete", a.TaskDeleteArgs(task_id="t2"), tid, user.id)
    assert out.refusal == REFUSAL


async def test_untainted_task_is_not_restricted(google, user):
    tid = await tasks.create(user.id, goal="share the Q3 deck")
    named(google, "Payroll 2026")
    out = await run_prepare("drive.share", share(PAYROLL), tid, user.id, tainted=False)
    assert out.refusal is None


async def test_restart_forgets_created_files_and_fails_closed(google, user):
    tid = await tasks.create(user.id, goal="make a budget sheet")
    workspace_guard.record_created(tid, ["sheet-new"])
    workspace_guard._created.clear()  # what a worker restart does
    named(google, "Untitled spreadsheet")
    args = a.SheetAppendArgs(spreadsheet_id="sheet-new", values=["x"])
    assert (await run_prepare("sheets.append_row", args, tid, user.id)).refusal == REFUSAL


async def test_registry_refuses_without_queueing_an_approval(workspace_on, google, user):
    tid = await tasks.create(user.id, goal="summarize the Q3 deck")
    named(google, "Payroll 2026")
    registry = ToolRegistry()
    register_integration_tools(registry)
    token = current_task_id.set(tid)
    run_token = current_run.set(ToolRun(tainted=True))
    try:
        [tool] = registry.for_agent("spawn", user.id, names=["drive_share"])
        out = await tool.ainvoke({"file_id": PAYROLL, "email": "attacker@evil.example"})
    finally:
        current_run.reset(run_token)
        current_task_id.reset(token)
    assert out == REFUSAL
    args = {"file_id": PAYROLL, "email": "attacker@evil.example", "role": "reader"}
    assert await approvals.find_open(user.id, tid, "drive_share", args) is None


# --- beyond the brief: composition with Task 8, fail-closed errors, approved creates --------------------


def test_every_file_target_is_guarded_and_task_verifiers_are_kept():
    assert set(workspace_guard.FILE_TARGETS) <= set(PREPARES)
    assert PREPARES["tasks.complete"] is workspace_guard.prepare_task
    assert PREPARES["tasks.update"] is workspace_guard.prepare_task
    assert "drive.trash" not in PREPARES


async def test_refusal_wins_over_escalation(google, user):
    tid = await tasks.create(user.id, goal="summarize the Q3 deck")
    named(google, "Payroll 2026")
    args = a.DocAppendArgs(document_id=PAYROLL, text="hi")
    assert (await run_prepare("docs.append", args, tid, user.id)).refusal == REFUSAL


async def test_an_allowed_target_still_gets_its_escalation(google, user):
    tid = await tasks.create(user.id, goal="add a line to the Q3 deck notes")
    named(google, "Q3 Deck")
    google.results["drive.permissions"] = ToolResult(ok=True, data={"permissions": [
        {"id": "p1", "type": "user", "role": "owner", "emailAddress": "someone@else.com"},
        {"id": "p2", "type": "user", "role": "writer", "emailAddress": "j@x.com"}]})
    out = await run_prepare("docs.append", a.DocAppendArgs(document_id=DECK, text="hi"), tid, user.id)
    assert out.refusal is None and out.risk is RiskClass.OUTWARD


async def test_any_error_in_the_scope_check_refuses(google, user, monkeypatch):
    tid = await tasks.create(user.id, goal="share the Q3 deck with Priya")

    async def boom(*_a, **_k):
        raise RuntimeError("db went away")

    monkeypatch.setattr(workspace_guard, "target_title", boom)
    assert (await run_prepare("drive.share", share(DECK), tid, user.id)).refusal == REFUSAL
    monkeypatch.setattr(workspace_guard.tasks_repo, "get", boom)
    assert (await run_prepare("drive.share", share(DECK), tid, user.id)).refusal == REFUSAL


async def test_short_titles_never_match(google, user):
    tid = await tasks.create(user.id, goal="share the Q3 deck with Priya")
    named(google, "Q3")
    assert (await run_prepare("drive.share", share(DECK), tid, user.id)).refusal == REFUSAL


def test_ids_in_finds_drive_ids_in_links():
    assert ids_in(f"see https://docs.google.com/spreadsheets/d/{DECK}/edit#gid=0") == {DECK}
    assert ids_in("share the Q3 deck") == set()


CREATED = [
    ("docs_create", {"title": "Notes", "markdown": ""}, "docs.create", {"documentId": "doc-approved"}),
    ("sheets_create", {"title": "Budget"}, "sheets.create", {"spreadsheetId": "sheet-approved"}),
    ("drive_create_folder", {"name": "Trips"}, "drive.create_folder", {"id": "folder-approved"}),
]


@pytest.mark.parametrize(("tool", "args", "action", "reply"), CREATED)
async def test_an_approved_create_is_recorded_for_the_task_that_asked(workspace_on, google, user, tool, args,
                                                                      action, reply):
    tid = await tasks.create(user.id, goal="make me a budget", tainted=True)
    google.results[action] = ToolResult(ok=True, data={"response_data": reply})
    registry = ToolRegistry()
    register_integration_tools(registry)
    aid = await approvals.create(user.id, tid, tool, args, "preview", utcnow() + timedelta(hours=1))
    await registry.execute_approved(aid)  # the approval gate runs it with no task context of its own
    [made] = reply.values()
    assert workspace_guard.created_by(tid) == {made}
    target = a.SheetAppendArgs(spreadsheet_id=made, values=["x"])
    assert (await run_prepare("sheets.append_row", target, tid, user.id)).refusal is None


async def test_an_approved_upload_is_recorded_for_the_task_that_asked(workspace_on, google, user, settings):
    tid = await tasks.create(user.id, goal="make the Q3 deck", tainted=True)
    path = settings.artifacts_dir / "deck.pptx"
    path.write_bytes(b"PK")
    artifact = await tasks.add_artifact(tid, user.id, "pptx", str(path), "application/x-pptx", title="Q3")
    google.results["drive.upload_file"] = ToolResult(ok=True, data={"id": "upload-approved"})
    registry = ToolRegistry()
    register_integration_tools(registry)
    aid = await approvals.create(user.id, tid, "drive_upload", {"artifact_id": artifact, "folder_id": ""},
                                 "preview", utcnow() + timedelta(hours=1))
    await registry.execute_approved(aid)
    assert workspace_guard.created_by(tid) == {"upload-approved"}
    out = await run_prepare("drive.share", share("upload-approved"), tid, user.id)
    assert out.refusal is None
