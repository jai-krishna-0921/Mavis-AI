"""Machine tools end to end on the real AgentCore code interpreter (opt-in: MAVIS_LIVE_AGENTCORE=1, AWS
credentials; costs cents). Covers code, an offline package install and a document builder."""

from __future__ import annotations

import os

import pytest

from mavis import machine
from mavis.machine.agentcore import AgentCoreSandbox
from mavis.machine.fake import MemoryWorkspaceStore
from mavis.machine.runtime import MachineRuntime
from mavis.store.repo import tasks
from mavis.tools import machine_tools as mt
from mavis.tools.registry import ToolRun, current_run, current_task_id

pytestmark = pytest.mark.skipif(
    os.environ.get("MAVIS_LIVE_AGENTCORE") != "1", reason="live AgentCore is opt-in"
)


async def _call(reg, user, name, **kw):
    tool = reg.get(name)
    token = current_run.set(ToolRun())
    try:
        return await reg.invoke(tool, user.id, tool.args_model(**kw))
    finally:
        current_run.reset(token)


async def test_code_install_and_a_deck_on_the_real_machine(db, user, fresh_registry, monkeypatch):
    from mavis.machine import quota

    monkeypatch.setattr(quota, "get_redis", lambda: None)
    delivered = []

    async def deliver(user_id, task_id, artifact_id, **kw):
        delivered.append(artifact_id)
        return True

    rt = MachineRuntime(AgentCoreSandbox(), MemoryWorkspaceStore(), deliver=deliver)
    machine.set_runtime(rt)
    mt.register_machine_tools(fresh_registry)
    tid = await tasks.create(user.id, goal="live check")
    token = current_task_id.set(tid)
    try:
        out = await _call(
            fresh_registry,
            user,
            "machine_run_python",
            code=(
                "import os\nos.makedirs('out', exist_ok=True)\n"
                "open('out/n.txt','w').write(str(sum(range(101))))\nprint(sum(range(101)))"
            ),
            purpose="sum",
        )
        assert "exit 0" in out and "5050" in out and "out/n.txt" in out
        out = await _call(
            fresh_registry,
            user,
            "make_pptx",
            title="Live",
            subtitle="check",
            slides=[{"title": "One", "bullets": ["a", "b"]}],
            filename="live.pptx",
        )
        assert "exit 0" in out and "out/live.pptx" in out, out
        assert (await rt.store.get(user.id, "out/live.pptx"))[:2] == b"PK"
        # fpdf2 is not on the machine: this installs its wheels offline first
        out = await _call(
            fresh_registry,
            user,
            "make_pdf",
            title="Live",
            sections=[{"heading": "H", "paragraphs": ["p"]}],
            filename="live.pdf",
        )
        assert "exit 0" in out and "out/live.pdf" in out, out
        assert (await rt.store.get(user.id, "out/live.pdf"))[:4] == b"%PDF"
        assert len(delivered) == 3
    finally:
        current_task_id.reset(token)
        await rt.release(tid)
        machine.set_runtime(None)
