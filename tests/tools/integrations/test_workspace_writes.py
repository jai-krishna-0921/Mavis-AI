"""Workspace writes with a fixed risk: translations, risk classes, taint approval, created-id tracking."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from mavis.domain.errors import ActionFailed, ApprovalRequired
from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import Capability, RiskClass
from mavis.tools.integrations import actions as a
from mavis.tools.integrations import workspace_guard
from mavis.tools.integrations.actions import ACTIONS, WORKSPACE_CAPABILITIES
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS
from mavis.tools.integrations.tools import register_integration_tools
from mavis.tools.integrations.workspace_guard import TASK_UNKNOWN, prepare_task
from mavis.tools.integrations.workspace_render import render_meet
from mavis.tools.integrations.workspace_tools import creating, tasks_complete, tasks_update
from mavis.tools.registry import TaintPolicy, ToolContext, ToolRegistry, ToolRun, current_run


def tr(action: str, args) -> dict:
    return COMPOSIO_ACTIONS[action].translate(args)


def test_write_translations_golden():
    assert tr("drive.create_folder", a.FolderCreateArgs(name="Trips")) == {"folder_name": "Trips"}
    assert tr("drive.move", a.DriveMoveArgs(file_id="f1", to_folder_id="d2", from_folder_id="d1")) == {
        "file_id": "f1", "add_parents": "d2", "remove_parents": "d1",
    }
    assert tr("drive.share", a.DriveShareArgs(file_id="f1", email="priya@example.com")) == {
        "file_id": "f1", "role": "reader", "type": "user", "email_address": "priya@example.com",
    }
    assert tr("docs.create", a.DocCreateArgs(title="Notes", markdown="# Hi")) == {
        "title": "Notes", "markdown_text": "# Hi",
    }
    assert tr("docs.comment", a.DocCommentArgs(file_id="f1", content="Looks good")) == {
        "file_id": "f1", "content": "Looks good",
    }
    assert tr("sheets.create", a.SheetCreateArgs(title="Budget")) == {"title": "Budget"}
    assert tr("tasks.add", a.TaskAddArgs(title="Pay rent", due=date(2026, 10, 5))) == {
        "tasklist_id": "@default", "title": "Pay rent", "status": "needsAction",
        "due": "2026-10-05T00:00:00.000Z",
    }
    assert tr("tasks.patch", a.TaskPatchArgs(task_id="t1", title="Pay rent", status="completed")) == {
        "tasklist_id": "@default", "task_id": "t1", "title": "Pay rent", "status": "completed",
    }
    assert tr("tasks.patch", a.TaskPatchArgs(task_id="t1", title="Pay rent", status="needsAction", notes="",
                                             due=date(2026, 10, 5))) == {
        "tasklist_id": "@default", "task_id": "t1", "title": "Pay rent", "status": "needsAction", "notes": "",
        "due": "2026-10-05T00:00:00.000Z",
    }
    # complete and update read the real task first (workspace_tools), so they have no direct mapping
    assert "tasks.complete" not in COMPOSIO_ACTIONS and "tasks.update" not in COMPOSIO_ACTIONS
    assert tr("tasks.delete", a.TaskDeleteArgs(task_id="t1")) == {"tasklist_id": "@default", "task_id": "t1"}
    assert tr("meet.create", a.NoArgs()) == {}


def test_share_requires_an_email_address():
    with pytest.raises(ValueError):
        a.DriveShareArgs(file_id="f1", email="anyone")


def test_risk_classes():
    assert ACTIONS["drive.share"].risk is RiskClass.OUTWARD
    assert ACTIONS["docs.comment"].risk is RiskClass.OUTWARD
    assert ACTIONS["tasks.delete"].risk is RiskClass.DESTRUCTIVE
    for name in ("drive.create_folder", "drive.move", "docs.create", "sheets.create", "tasks.add",
                 "tasks.complete", "tasks.update", "meet.create"):
        assert ACTIONS[name].risk is RiskClass.WRITE_SELF and ACTIONS[name].taint_approve, name


def test_every_workspace_write_self_action_needs_approval_when_tainted():
    for spec in ACTIONS.values():
        if spec.capability in WORKSPACE_CAPABILITIES and spec.risk is RiskClass.WRITE_SELF and spec.agents:
            assert spec.taint_approve, spec.name


def test_chat_exposure_is_reads_plus_three_writes():
    chat_writes = {n for n, s in ACTIONS.items()
                   if "conversation" in s.agents and s.capability in WORKSPACE_CAPABILITIES
                   and s.risk is not RiskClass.READ}
    assert chat_writes == {"docs.create", "tasks.add", "tasks.complete"}


async def test_tainted_run_queues_docs_create_for_approval(workspace_on, user):
    registry = ToolRegistry()
    register_integration_tools(registry)
    tool = registry.get("docs_create")
    assert tool.on_taint is TaintPolicy.APPROVE
    token = current_run.set(ToolRun(tainted=True))
    try:
        with pytest.raises(ApprovalRequired) as exc:
            await registry.invoke(tool, user.id, a.DocCreateArgs(title="Notes", markdown="hi"))
    finally:
        current_run.reset(token)
    assert exc.value.preview.startswith("📄 New Google Doc: Notes")


async def test_untainted_docs_create_runs_without_approval(workspace_on, user):
    ran: list[str] = []

    async def fake(user_id, args):
        ran.append(args.title)
        return "Done."

    registry = ToolRegistry()
    register_integration_tools(registry)
    tool = dataclasses.replace(registry.get("docs_create"), fn=fake)
    out = await registry.invoke(tool, user.id, a.DocCreateArgs(title="Notes"))
    assert out == '<untrusted source="docs_create">\nDone.\n</untrusted>' and ran == ["Notes"]


async def test_creating_records_ids_for_the_task(provider, cache):
    provider.set_state(1, Capability.DOCS, ConnectionState.ACTIVE)
    provider.results["docs.create"] = ToolResult(ok=True, data={"document_id": "doc-new", "request_data": []})
    workspace_guard._created.clear()
    out = await creating("docs.create", provider=provider, cache=cache)(
        ToolContext(user_id=1, task_id=42), a.DocCreateArgs(title="Notes")
    )
    assert out == 'Done. {"document_id": "doc-new"}'
    assert workspace_guard.created_by(42) == {"doc-new"}
    assert workspace_guard.created_by(43) == set()


def test_render_meet_returns_only_a_google_meet_link():
    assert render_meet({"response_data": {"meetingUri": "https://meet.google.com/abc-defg-hij"}}) == (
        "Meet link: https://meet.google.com/abc-defg-hij"
    )
    other = render_meet({"response_data": {"meetingUri": "https://evil.example/x"}})
    assert other.startswith("Created the Meet")


# --- tasks.complete / tasks.update: the real title comes from tasks.get, never from the model ----------


@pytest.fixture
def gtasks(provider, cache, monkeypatch, user):
    from mavis.tools.integrations import tools as tools_mod

    provider.set_state(user.id, Capability.TASKS, ConnectionState.ACTIVE)
    monkeypatch.setattr(tools_mod, "_deps", lambda p, c: (provider, cache))
    provider.results["tasks.get"] = ToolResult(ok=True, data={"response_data": {
        "id": "t1", "title": "Pay rent", "status": "needsAction"}})
    return provider


def patched(provider) -> dict:
    calls = [e[2] for e in provider.executed if e[1] == "tasks.patch"]
    assert len(calls) == 1
    return calls[0]


def test_task_args_carry_no_model_title_for_completion():
    assert "title" not in a.TaskCompleteArgs.model_fields
    assert a.TaskUpdateArgs(task_id="t1").done is None and a.TaskUpdateArgs(task_id="t1").title is None


async def test_complete_patches_with_the_verified_title(gtasks, user):
    out = await tasks_complete(ToolContext(user_id=user.id), a.TaskCompleteArgs(task_id="t1"))
    assert patched(gtasks) == {"task_id": "t1", "title": "Pay rent", "status": "completed", "notes": None,
                               "due": None}
    assert out.startswith("Done.")


async def test_editing_a_completed_task_does_not_reopen_it(gtasks, user):
    gtasks.results["tasks.get"] = ToolResult(ok=True, data={"title": "Pay rent", "status": "completed"})
    await tasks_update(ToolContext(user_id=user.id), a.TaskUpdateArgs(task_id="t1", notes="paid by UPI"))
    assert patched(gtasks) == {"task_id": "t1", "title": "Pay rent", "status": "completed",
                               "notes": "paid by UPI", "due": None}


async def test_update_can_rename_and_reopen(gtasks, user):
    await tasks_update(ToolContext(user_id=user.id),
                       a.TaskUpdateArgs(task_id="t1", title="Pay rent (Oct)", done=False))
    assert patched(gtasks)["title"] == "Pay rent (Oct)" and patched(gtasks)["status"] == "needsAction"


async def test_failed_task_lookup_never_patches(gtasks, user):
    gtasks.results["tasks.get"] = ToolResult(ok=False, error="Task not found")
    with pytest.raises(ActionFailed):
        await tasks_complete(ToolContext(user_id=user.id), a.TaskCompleteArgs(task_id="t1"))
    gtasks.results["tasks.get"] = ToolResult(ok=True, data={"response_data": {"id": "t1"}})  # no title
    with pytest.raises(ActionFailed):
        await tasks_update(ToolContext(user_id=user.id), a.TaskUpdateArgs(task_id="t1", notes="x"))
    assert "tasks.patch" not in [e[1] for e in gtasks.executed]


async def test_prepare_task_notes_the_verified_title_or_refuses(gtasks, user):
    ctx = ToolContext(user_id=user.id)
    out = await prepare_task(ctx, a.TaskCompleteArgs(task_id="t1"))
    assert out.note == "Task: Pay rent" and out.refusal is None and out.risk is None
    gtasks.results["tasks.get"] = ToolResult(ok=False, error="Task not found")
    refused = await prepare_task(ctx, a.TaskUpdateArgs(task_id="t9", notes="x"))
    assert refused.refusal == TASK_UNKNOWN


async def test_tainted_task_previews_show_the_real_title(workspace_on, gtasks, user):
    registry = ToolRegistry()
    register_integration_tools(registry)
    token = current_run.set(ToolRun(tainted=True))
    try:
        with pytest.raises(ApprovalRequired) as done:
            await registry.invoke(registry.get("tasks_complete"), user.id, a.TaskCompleteArgs(task_id="t1"))
        with pytest.raises(ApprovalRequired) as edit:
            await registry.invoke(registry.get("tasks_update"), user.id,
                                  a.TaskUpdateArgs(task_id="t1", title="Rent", due=date(2026, 10, 9)))
    finally:
        current_run.reset(token)
    assert done.value.preview == "✅ Mark a task done\nTask: Pay rent"
    assert edit.value.preview == "✅ Update a task\nNew title: Rent\nDue: Fri 09 Oct\nTask: Pay rent"
    assert "tasks.patch" not in [e[1] for e in gtasks.executed]


async def test_a_second_task_edit_in_one_run_sees_the_first(gtasks, user):
    ctx = ToolContext(user_id=user.id)
    token = current_run.set(ToolRun())
    try:
        await tasks_update(ctx, a.TaskUpdateArgs(task_id="t1", done=True))
        await tasks_update(ctx, a.TaskUpdateArgs(task_id="t1", notes="x"))
        await tasks_update(ctx, a.TaskUpdateArgs(task_id="t1", title="New"))
        await tasks_complete(ctx, a.TaskCompleteArgs(task_id="t1"))
        note = (await prepare_task(ctx, a.TaskCompleteArgs(task_id="t1"))).note
    finally:
        current_run.reset(token)
    sent = [(e[2]["title"], e[2]["status"]) for e in gtasks.executed if e[1] == "tasks.patch"]
    assert sent == [("Pay rent", "completed"), ("Pay rent", "completed"), ("New", "completed"),
                    ("New", "completed")]  # never reopened, rename kept
    assert note == "Task: New"
    assert [e[1] for e in gtasks.executed].count("tasks.get") == 1
