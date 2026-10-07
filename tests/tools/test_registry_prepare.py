"""MavisTool.prepare: an async pre-step that may escalate risk or refuse, before taint and approval
(Workspace spec 4.1). execute_approved never runs it."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel

from mavis.domain.errors import ApprovalRequired
from mavis.domain.policy import RiskClass
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks
from mavis.tools.registry import (
    MavisTool,
    Prepared,
    TaintPolicy,
    ToolContext,
    ToolRegistry,
    ToolRun,
    current_run,
)


class FileArgs(BaseModel):
    file_id: str


def _tool(prepare, ran: list[str], risk=RiskClass.WRITE_SELF, **kw) -> MavisTool:
    async def fn(user_id: int, args: FileArgs) -> str:
        ran.append(args.file_id)
        return "done"

    return MavisTool(name="append", description="append", args_model=FileArgs, risk=risk, fn=fn,
                     agents=frozenset({"spawn"}), prepare=prepare, preview=lambda a: f"Append to {a.file_id}",
                     **kw)


async def test_escalation_to_outward_queues_approval(user):
    ran: list[str] = []

    async def shared(ctx: ToolContext, args: FileArgs) -> Prepared:
        return Prepared(risk=RiskClass.OUTWARD)

    reg = ToolRegistry()
    tool = _tool(shared, ran)
    reg.register(tool)
    with pytest.raises(ApprovalRequired) as exc:
        await reg.invoke(tool, user.id, FileArgs(file_id="f1"))
    assert exc.value.preview == "Append to f1" and ran == []


async def test_note_is_appended_to_the_approval_preview(user):
    async def verified(ctx: ToolContext, args: FileArgs) -> Prepared:
        return Prepared(risk=RiskClass.OUTWARD, note="File: Budget 2026 (Sheet), shared with 3 people")

    reg = ToolRegistry()
    tool = _tool(verified, [])
    reg.register(tool)
    with pytest.raises(ApprovalRequired) as exc:
        await reg.invoke(tool, user.id, FileArgs(file_id="f1"))
    assert exc.value.preview == "Append to f1\nFile: Budget 2026 (Sheet), shared with 3 people"


async def test_no_escalation_runs_write_self(user):
    ran: list[str] = []

    async def mine(ctx: ToolContext, args: FileArgs) -> Prepared:
        return Prepared()

    reg = ToolRegistry()
    tool = _tool(mine, ran)
    reg.register(tool)
    assert await reg.invoke(tool, user.id, FileArgs(file_id="f1")) == "done" and ran == ["f1"]


async def test_prepare_cannot_lower_risk(user):
    ran: list[str] = []

    async def lower(ctx: ToolContext, args: FileArgs) -> Prepared:
        return Prepared(risk=RiskClass.READ)

    reg = ToolRegistry()
    tool = _tool(lower, ran, risk=RiskClass.DESTRUCTIVE)
    reg.register(tool)
    with pytest.raises(ApprovalRequired):
        await reg.invoke(tool, user.id, FileArgs(file_id="f1"))


async def test_failing_prepare_fails_closed(user):
    ran: list[str] = []

    async def boom(ctx: ToolContext, args: FileArgs) -> Prepared:
        raise RuntimeError("metadata lookup failed")

    reg = ToolRegistry()
    tool = _tool(boom, ran)
    reg.register(tool)
    with pytest.raises(ApprovalRequired):
        await reg.invoke(tool, user.id, FileArgs(file_id="f1"))
    assert ran == []


async def test_refusal_comes_before_taint_approval(user):
    ran: list[str] = []

    async def refuse(ctx: ToolContext, args: FileArgs) -> Prepared:
        return Prepared(refusal="Refused: not a file the user named.")

    reg = ToolRegistry()
    tool = _tool(refuse, ran, on_taint=TaintPolicy.APPROVE)
    reg.register(tool)
    token = current_run.set(ToolRun(tainted=True))
    try:
        out = await reg.invoke(tool, user.id, FileArgs(file_id="f1"))
    finally:
        current_run.reset(token)
    assert out == "Refused: not a file the user named." and ran == []


async def test_prepare_sees_the_user_context(user):
    seen: list[ToolContext] = []

    async def spy(ctx: ToolContext, args: FileArgs) -> Prepared:
        seen.append(ctx)
        return Prepared()

    reg = ToolRegistry()
    tool = _tool(spy, [])
    reg.register(tool)
    await reg.invoke(tool, user.id, FileArgs(file_id="f1"))
    assert seen[0].user_id == user.id


async def test_execute_approved_skips_prepare(user):
    ran: list[str] = []
    calls: list[str] = []

    async def spy(ctx: ToolContext, args: FileArgs) -> Prepared:
        calls.append(args.file_id)
        return Prepared(refusal="Refused")

    reg = ToolRegistry()
    reg.register(_tool(spy, ran))
    tid = await tasks.create(user.id, goal="g")
    aid = await approvals.create(user.id, tid, "append", {"file_id": "f1"}, "Append to f1",
                                 utcnow() + timedelta(hours=1))
    assert (await reg.execute_approved(aid)).text == "done"
    assert ran == ["f1"] and calls == []


def test_tool_run_has_a_memo():
    assert ToolRun().memo == {}

