"""Workspace writes with a fixed risk: translations, risk classes, taint approval, created-id tracking."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from mavis.domain.errors import ActionFailed
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
        assert ACTIONS[name].risk is RiskClass.WRITE_SELF, name


def test_only_contact_changes_need_a_tap_after_third_party_content():
    """Owner decision (2026-10-10): self-only Workspace writes run without a card. A contact change still
    asks after mail or file content was read: an injected edit could swap an address for an attacker's,
    and later mail would go there. Shared files are escalated to OUTWARD by their own pre-steps."""
    tapped = {spec.name for spec in ACTIONS.values()
              if spec.capability in WORKSPACE_CAPABILITIES and spec.risk is RiskClass.WRITE_SELF
              and spec.agents and spec.taint_approve}
    assert tapped == {"contacts.create", "contacts.update"}


def test_chat_exposure_is_reads_plus_the_self_only_writes():
    chat_writes = {n for n, s in ACTIONS.items()
                   if "conversation" in s.agents and s.capability in WORKSPACE_CAPABILITIES
                   and s.risk is not RiskClass.READ}
    # chat can do what the user asks in one turn; self-only writes need a tap when tainted, the rest always
    assert chat_writes == {"docs.create", "docs.append", "tasks.add", "tasks.complete", "tasks.update",
                           "slides.create", "contacts.create", "contacts.update", "sheets.create",
                           "sheets.append_row", "sheets.update_range", "drive.create_folder", "drive.move",
                           "drive.share", "meet.create"}
    for name in chat_writes:
        spec = ACTIONS[name]
        assert spec.risk is RiskClass.OUTWARD or spec.taint_approve or spec.risk is RiskClass.WRITE_SELF, name


async def test_tainted_run_creates_a_doc_without_a_card(workspace_on, user):
    registry = ToolRegistry()
    register_integration_tools(registry)
    tool = registry.get("docs_create")
    assert tool.on_taint is TaintPolicy.ALLOW
    assert registry.get("contacts_update").on_taint is TaintPolicy.APPROVE


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
    assert out.model_note == 'Done. {"document_id": "doc-new"}'
    assert out.user_text == ""  # "doc-new" is not a Drive id, so no link is built for it
    assert await workspace_guard.created_by(42) == {"doc-new"}
    assert await workspace_guard.created_by(43) == set()


def test_render_meet_returns_only_a_google_meet_link():
    meet = render_meet({"response_data": {"meetingUri": "https://meet.google.com/abc-defg-hij"}})
    assert meet.user_text == "Meet link: https://meet.google.com/abc-defg-hij"
    other = render_meet({"response_data": {"meetingUri": "https://evil.example/x"}})
    assert other.user_text.startswith("Created the Meet")


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


async def test_task_previews_show_the_real_title(workspace_on, gtasks, user):
    registry = ToolRegistry()
    register_integration_tools(registry)
    ctx = ToolContext(user_id=user.id)
    done_args = a.TaskCompleteArgs(task_id="t1")
    edit_args = a.TaskUpdateArgs(task_id="t1", title="Rent", due=date(2026, 10, 9))
    done = registry.get("tasks_complete")
    edit = registry.get("tasks_update")
    done_preview = done.render_preview(done_args, ctx) + "\n" + (await done.prepare(ctx, done_args)).note
    edit_preview = edit.render_preview(edit_args, ctx) + "\n" + (await edit.prepare(ctx, edit_args)).note
    assert done_preview == "✅ Mark a task done\nTask: Pay rent"
    assert edit_preview == "✅ Update a task\nNew title: Rent\nDue: Fri 09 Oct\nTask: Pay rent"


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


async def test_creating_with_send_as_exports_the_new_file_on_the_same_approval(provider, cache, db, sent,
                                                                               settings):
    import base64

    fid = "1AbCdEfGhIjKlMn"
    provider.set_state(1, Capability.DOCS, ConnectionState.ACTIVE)
    provider.set_state(1, Capability.DRIVE, ConnectionState.ACTIVE)
    provider.results["docs.create"] = ToolResult(ok=True, data={"documentId": fid, "title": "Agenda"})
    provider.results["drive.meta"] = ToolResult(ok=True, data={
        "name": "Agenda", "mimeType": "application/vnd.google-apps.document"})
    provider.results["drive.export_file"] = ToolResult(ok=True, data={
        "content_b64": base64.b64encode(b"%PDF").decode()})
    out = await creating("docs.create", provider=provider, cache=cache)(
        ToolContext(user_id=1, task_id=7), a.DocCreateArgs(title="Agenda", send_as="pdf"))
    assert out.user_text == f"Created Agenda: https://docs.google.com/document/d/{fid}/edit"
    assert "SENT: Agenda.pdf" in out.model_note
    assert [m.text for m in sent] == ["Agenda.pdf"]


def test_the_card_says_the_file_will_be_sent():
    preview = a.ACTIONS["docs.create"].preview(a.DocCreateArgs(title="Agenda", send_as="pdf"), "UTC")
    assert preview.startswith("📄 New Google Doc: Agenda\nThen sent to you here as a PDF file.")


def test_doc_cards_show_plain_text_not_markdown_marks():
    preview = ACTIONS["docs.create"].preview(
        a.DocCreateArgs(title="Agenda", markdown="# Agenda\n**Date:** Sat\n## Goal\nShip `it`"), "UTC")
    assert preview == "📄 New Google Doc: Agenda\nAgenda\nDate: Sat\nGoal\nShip it"
