from __future__ import annotations

from mavis.agents.specialists import SPECIALISTS
from mavis.machine.wiring import register_machine


def test_machine_specialists_only_when_enabled(settings, monkeypatch):
    SPECIALISTS.pop("analyst", None)
    SPECIALISTS.pop("docs", None)
    register_machine()
    assert "analyst" not in SPECIALISTS
    monkeypatch.setattr(settings, "machine_enabled", True)
    monkeypatch.setattr(settings, "sandbox_backend", "fake")
    register_machine()
    assert SPECIALISTS["analyst"].machine and SPECIALISTS["docs"].machine
    assert SPECIALISTS["analyst"].steps_setting == "analyst_max_steps"
    for name in ("analyst", "docs"):
        assert "conversation" not in name


async def test_what_i_ran_ends_the_result_of_a_task_that_ran_code(db, user, settings, monkeypatch):
    from mavis import machine
    from mavis.agents.orchestrator_graph import complete_task
    from mavis.domain.tasks import TaskStatus
    from mavis.machine import quota
    from mavis.machine.fake import FakeSandbox, MemoryWorkspaceStore
    from mavis.machine.ports import ExecRequest, ExecResult
    from mavis.machine.runtime import MachineRuntime
    from mavis.store.repo import tasks

    monkeypatch.setattr(quota, "get_redis", lambda: None)
    sandbox = FakeSandbox()
    sandbox.on_exec = lambda req, files: ExecResult(ok=True, exit_code=0, stdout="total: 42\n")
    rt = MachineRuntime(sandbox, MemoryWorkspaceStore())
    machine.set_runtime(rt)
    try:
        tid = await tasks.create(user.id, goal="sum it")
        await rt.exec(user.id, tid, ExecRequest(language="python", code="print(42)", timeout_s=20))
        state = {"task_id": tid, "user_id": user.id, "artifacts": []}
        assert await complete_task(state, ["The total is 42."], TaskStatus.DONE, None)
        text = (await tasks.get(tid)).result_text
        assert text.startswith("The total is 42.") and "What I ran:" in text and "total: 42" in text
        assert "—" not in text
    finally:
        machine.set_runtime(None)


def test_chat_is_told_about_the_machine_only_for_allowed_users(settings, monkeypatch):
    from mavis import machine
    from mavis.agents import conversation
    from mavis.machine.fake import FakeSandbox, MemoryWorkspaceStore
    from mavis.machine.runtime import MachineRuntime

    assert conversation.machine_rule(1) == ""  # flag off
    monkeypatch.setattr(settings, "machine_enabled", True)
    machine.set_runtime(MachineRuntime(FakeSandbox(), MemoryWorkspaceStore()))
    try:
        rule = conversation.machine_rule(1)
        assert "start_task" in rule and "—" not in rule
        monkeypatch.setattr(settings, "machine_users", [2])
        assert conversation.machine_rule(1) == ""
    finally:
        machine.set_runtime(None)
