# ruff: noqa: E501
"""End to end: a scripted model drives a real react loop over the real registry and a fake MCP server on a
local socket. A read runs on its own and taints the run; an act queues a card, runs only when approved, and
the same server never sees a second attempt."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mavis.agents.react import react_loop
from mavis.config import get_settings
from mavis.domain.errors import ActionFailed
from mavis.domain.tasks import ApprovalStatus, TaskKind
from mavis.mcp import service as svc
from mavis.mcp.service import McpService
from mavis.store.repo import approvals, tasks
from mavis.store.repo import mcp_servers as repo
from mavis.tools.registry import ToolRegistry, current_task_id
from tests.mcp.fake_server import text_result
from tests.mcp.test_service import open_server


def call(name: str, args: dict, cid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def tool_texts(res) -> list[str]:
    return [m.content for m in res.messages if getattr(m, "type", "") == "tool"]


async def run_loop(registry: ToolRegistry, user_id: int, fake_llm, calls: list[AIMessage], tid: int):
    tools = registry.for_agent("conversation", user_id)
    for c in calls:
        fake_llm.push_ai(c)
    fake_llm.push_text("Done what I can.")
    token = current_task_id.set(tid)
    try:
        return await react_loop(tools, [HumanMessage("use my notes")], max_steps=len(calls) + 3)
    finally:
        current_task_id.reset(token)


@pytest.fixture
def registry(mcp_env, fresh_registry) -> ToolRegistry:
    """The process registry, filled the way the app fills it (flag on)."""
    from mavis.tools import load_builtin_tools

    load_builtin_tools(fresh_registry)
    return fresh_registry


@pytest.fixture
async def notes(two_users, registry):
    a, b = two_users
    service = McpService()
    svc.set_service(service)
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        yield a, b, fake


ACT = {"server": "notes", "tool": "create_note", "arguments": {"title": "Hi"}}


async def test_read_then_act_through_the_loop_and_the_approval_path(notes, registry, fake_llm) -> None:
    a, _, fake = notes
    tid = await tasks.create(a.id, goal="g", kind=TaskKind.APPROVAL)
    res = await run_loop(registry, a.id, fake_llm, [
        call("mcp_tools", {}, "c1"),
        call("mcp_read", {"server": "notes", "tool": "get_note", "arguments": {"id": "7"}}, "c2"),
        call("mcp_act", ACT, "c3")], tid)
    assert res.tools_called == ["mcp_tools", "mcp_read", "mcp_act"]
    assert res.tainted is True  # third-party text reached the model
    listing, read, act = tool_texts(res)
    assert "get_note [read]" in listing and "create_note [act]" in listing
    assert "note 7" in read and act.startswith("QUEUED_FOR_APPROVAL #")
    assert fake.calls == [("get_note", {"id": "7"})]  # the read ran with no card; the act has NOT run
    pending = await approvals.next_open(tid)
    assert pending.tool == "mcp_act" and pending.preview.startswith("Run create_note on notes\nArguments:\n  - title: Hi")
    await resume_gate(tid, pending.id, "ok")
    assert (await approvals.get(pending.id)).status == ApprovalStatus.EXECUTED
    assert "made Hi" in (await approvals.get(pending.id)).result
    assert fake.calls == [("get_note", {"id": "7"}), ("create_note", {"title": "Hi"})]
    assert fake.attempts == 2  # the read and the one act, nothing else


async def resume_gate(tid: int, approval_id: int, decision: str):
    """Drive the real approval_gate of the orchestrator graph the way the approval callback does."""
    from langgraph.types import Command

    from mavis.agents import orchestrator_graph as og
    from tests.agents.test_orchestrator_graph import _cfg, _graph

    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    await approvals.claim(approval_id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    await graph.ainvoke(Command(resume={"approval_id": approval_id, "decision": decision}), _cfg(tid))


async def queued_card(registry, fake_llm, user_id: int):
    tid = await tasks.create(user_id, goal="approve", kind=TaskKind.APPROVAL)
    await run_loop(registry, user_id, fake_llm, [call("mcp_act", ACT, "c1")], tid)
    return tid, await approvals.next_open(tid)


async def test_two_resumes_racing_for_one_approval_run_the_call_once(notes, registry, fake_llm) -> None:
    """Two gate runs (two graph threads, as when a duplicate tap and a retry both resume the same task) both
    hold the card in RESOLVING; only the one that wins the RESOLVING to EXECUTED claim may run the tool."""
    import asyncio

    from langgraph.types import Command

    from mavis.agents import orchestrator_graph as og
    from tests.agents.test_orchestrator_graph import _cfg, _graph

    a, _, fake = notes
    tid, pending = await queued_card(registry, fake_llm, a.id)
    graphs = [(_graph(), _cfg(tid, f":{i}")) for i in range(2)]
    for graph, cfg in graphs:  # both reach the gate and wait on the same open card
        await graph.ainvoke(og.initial_state(await tasks.get(tid)), cfg)
    assert await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    resume = Command(resume={"approval_id": pending.id, "decision": "ok"})
    await asyncio.gather(*(graph.ainvoke(resume, cfg) for graph, cfg in graphs))
    assert fake.calls == [("create_note", {"title": "Hi"})] and fake.attempts == 1
    assert (await approvals.get(pending.id)).status == ApprovalStatus.EXECUTED


async def test_cancelling_through_the_real_gate_executes_nothing(notes, registry, fake_llm) -> None:
    a, _, fake = notes
    tid, pending = await queued_card(registry, fake_llm, a.id)
    await resume_gate(tid, pending.id, "no")
    assert (await approvals.get(pending.id)).status == ApprovalStatus.REJECTED
    assert fake.calls == [] and fake.attempts == 0


async def test_an_act_the_tool_reports_as_an_error_is_a_failed_card(notes, registry, fake_llm) -> None:
    a, _, fake = notes
    fake.handlers["create_note"] = lambda args: text_result("quota exceeded", is_error=True)
    tid = await tasks.create(a.id, goal="g")
    await run_loop(registry, a.id, fake_llm, [call("mcp_act", ACT, "c1")], tid)
    pending = await approvals.next_open(tid)
    with pytest.raises(ActionFailed) as err:
        await registry.execute_approved(pending.id)
    assert "quota exceeded" in str(err.value) and fake.attempts == 1


async def test_a_read_that_is_an_error_result_reaches_the_model_fenced_and_is_not_retried(notes, registry, fake_llm) -> None:
    a, _, fake = notes
    fake.handlers["get_note"] = lambda args: text_result("no such note", is_error=True)
    tid = await tasks.create(a.id, goal="g")
    res = await run_loop(registry, a.id, fake_llm, [
        call("mcp_read", {"server": "notes", "tool": "get_note", "arguments": {"id": "9"}}, "c1")], tid)
    assert tool_texts(res) == ['<untrusted source="mcp_read">\nno such note\n</untrusted>']
    assert fake.attempts == 1 and res.text == "Done what I can."


async def test_oversized_output_is_capped_through_the_loop(notes, registry, fake_llm) -> None:
    a, _, fake = notes
    fake.handlers["get_note"] = lambda args: text_result("H" * 4000 + "M" * 60_000 + "T" * 4000)
    tid = await tasks.create(a.id, goal="g")
    res = await run_loop(registry, a.id, fake_llm, [
        call("mcp_read", {"server": "notes", "tool": "get_note", "arguments": {"id": "1"}}, "c1")], tid)
    out = tool_texts(res)[0]
    assert len(out) <= 12_000 + len('<untrusted source="mcp_read">\n\n</untrusted>') and "RESULT TRUNCATED" in out and "HHHH" in out and "TTTT" in out
    assert "M" * 6000 not in out


async def test_a_401_marks_the_server_needs_auth_and_stops_the_model(notes, registry, fake_llm) -> None:
    a, _, fake = notes
    fake.always_status = 401
    tid = await tasks.create(a.id, goal="g")
    res = await run_loop(registry, a.id, fake_llm, [
        call("mcp_read", {"server": "notes", "tool": "get_note", "arguments": {"id": "1"}}, "c1")], tid)
    assert "needs to be reconnected in the dashboard; do not retry" in tool_texts(res)[0]
    row = (await repo.list_for(a.id))[0]
    assert row.status == repo.NEEDS_AUTH


async def test_user_b_cannot_see_or_call_user_a_server(notes, registry, fake_llm) -> None:
    a, b, fake = notes
    tid = await tasks.create(b.id, goal="g")
    res = await run_loop(registry, b.id, fake_llm, [
        call("mcp_tools", {}, "c1"),
        call("mcp_read", {"server": "notes", "tool": "get_note", "arguments": {"id": "1"}}, "c2"),
        call("mcp_act", ACT, "c3")], tid)
    listing, read, act = tool_texts(res)
    assert "No MCP servers are connected" in listing and "get_note" not in listing
    assert "no connected server called 'notes'" in read and "no connected server called 'notes'" in act
    assert await approvals.next_open(tid) is None
    assert fake.attempts == 0 and fake.calls == []


async def test_chat_offers_the_tools_only_to_the_user_with_an_active_server(notes, registry) -> None:
    from mavis.agents.conversation import chat_tools, unlinked_with_internal

    a, b, _ = notes
    names = {"mcp_tools", "mcp_read", "mcp_act"}

    async def offered(user_id: int) -> set[str]:
        unlinked = await unlinked_with_internal(user_id)
        return {t.name for t in chat_tools(user_id, "what is in my notes app", unlinked=unlinked)}

    assert names <= await offered(a.id)
    assert not names & await offered(b.id)


def test_the_flag_decides_whether_the_tools_are_registered(settings, monkeypatch) -> None:
    from mavis.tools import load_builtin_tools

    names = {"mcp_tools", "mcp_read", "mcp_act"}
    for value, expected in (("true", names), ("false", set())):
        monkeypatch.setenv("MCP_ENABLED", value)
        get_settings.cache_clear()
        reg = ToolRegistry()
        load_builtin_tools(reg)
        assert names & set(reg.names_for("conversation")) == expected
    get_settings.cache_clear()
