"""Workspace writes with a fixed risk: translations, risk classes, taint approval, created-id tracking."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from mavis.domain.errors import ApprovalRequired
from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import Capability, RiskClass
from mavis.tools.integrations import actions as a
from mavis.tools.integrations import workspace_guard
from mavis.tools.integrations.actions import ACTIONS, WORKSPACE_CAPABILITIES
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS
from mavis.tools.integrations.tools import register_integration_tools
from mavis.tools.integrations.workspace_render import render_meet
from mavis.tools.integrations.workspace_tools import creating
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
    assert tr("tasks.complete", a.TaskCompleteArgs(task_id="t1", title="Pay rent")) == {
        "tasklist_id": "@default", "task_id": "t1", "title": "Pay rent", "status": "completed",
    }
    assert tr("tasks.update", a.TaskUpdateArgs(task_id="t1", title="Pay rent", notes="")) == {
        "tasklist_id": "@default", "task_id": "t1", "title": "Pay rent", "status": "needsAction", "notes": "",
    }
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
