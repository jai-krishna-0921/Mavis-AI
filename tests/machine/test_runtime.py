"""The runtime owns sessions per task: quota and slots first, sync in, files out as they appear."""

from __future__ import annotations

import pytest

from mavis.machine.errors import MachineBusy, QuotaExceeded, SessionUserMismatch
from mavis.machine.fake import FakeSandbox, MemoryWorkspaceStore
from mavis.machine.ports import ExecRequest, ExecResult, Provenance
from mavis.machine.quota import DAILY_TEXT, GlobalSlots
from mavis.machine.runtime import MachineRuntime
from mavis.store.repo import machine as repo
from mavis.store.repo import tasks, users


@pytest.fixture
def delivered():
    return []


@pytest.fixture
async def rt(db, settings, delivered, monkeypatch):
    from mavis.machine import quota

    monkeypatch.setattr(quota, "get_redis", lambda: None)
    sb, store = FakeSandbox(), MemoryWorkspaceStore()

    async def deliver(user_id, task_id, artifact_id, proactive=False, **_kw):
        delivered.append((user_id, task_id, artifact_id))
        return True

    runtime = MachineRuntime(sb, store, slots=GlobalSlots(size=2), deliver=deliver)
    runtime.fake = sb
    return runtime


async def _task(chat: int, goal: str):
    u, _ = await users.get_or_create_by_chat(chat, "R")
    return u.id, await tasks.create(u.id, goal=goal)


def _writes(files: dict[str, bytes]):
    def on_exec(req: ExecRequest, fs: dict[str, bytes]) -> ExecResult:
        fs.update(files)
        return ExecResult(ok=True, exit_code=0, stdout="ok")
    return on_exec


async def test_new_out_files_become_artifacts_and_are_delivered_once(rt, delivered):
    uid, tid = await _task(93_001, "plot monthly revenue")
    rt.fake.on_exec = _writes({"out/revenue.png": b"\x89PNG1", "work/tmp.csv": b"a,b"})
    res = await rt.exec(uid, tid, ExecRequest(language="python", code="...", timeout_s=30))
    assert {c.path for c in res.changed} == {"out/revenue.png", "work/tmp.csv"}
    arts = await tasks.artifacts_for(tid)
    assert [a.path.endswith("revenue.png") for a in arts] == [True]
    assert delivered == [(uid, tid, arts[0].id)]
    await rt.exec(uid, tid, ExecRequest(language="python", code="print(1)", timeout_s=30))  # no change
    assert len(delivered) == 1
    assert (await rt.store.meta(uid, "work/tmp.csv")).provenance is Provenance.GENERATED_CLEAN


async def test_untrusted_session_marks_new_files_tainted(rt):
    uid, tid = await _task(93_002, "summarise an uploaded report")
    rt.mark_untrusted(tid)
    rt.fake.on_exec = _writes({"out/summary.txt": b"s"})
    await rt.exec(uid, tid, ExecRequest(language="python", code="...", timeout_s=30))
    assert (await rt.store.meta(uid, "out/summary.txt")).provenance is Provenance.GENERATED_TAINTED


async def test_workspace_is_synced_in_on_open(rt):
    uid, tid = await _task(93_003, "count words in my notes")
    await rt.store.put(uid, "work/notes.txt", b"one two", provenance=Provenance.GENERATED_CLEAN, cls=None)
    await rt.exec(uid, tid, ExecRequest(language="shell", code="wc -w work/notes.txt", timeout_s=30))
    [session] = rt.fake.sessions.values()
    assert session.files["work/notes.txt"] == b"one two"


async def test_one_session_per_task_and_none_shared(rt):
    uid, t1 = await _task(93_004, "a")
    _, t2 = await _task(93_004, "b")
    s1 = await rt.session(uid, t1)
    assert await rt.session(uid, t1) is s1
    await rt.release(t1)
    s2 = await rt.session(uid, t2)
    assert s2.id != s1.id


async def test_session_user_mismatch_raises(rt):
    uid, tid = await _task(93_005, "x")
    other, _ = await users.get_or_create_by_chat(93_999, "O")
    await rt.session(uid, tid)
    with pytest.raises(SessionUserMismatch):
        await rt.session(other.id, tid)


async def test_quota_refusal_opens_nothing(rt):
    uid, tid = await _task(93_006, "y")
    await repo.set_quota(uid, "daily_minutes", 0)
    with pytest.raises(QuotaExceeded) as info:
        await rt.session(uid, tid)
    assert info.value.user_text == DAILY_TEXT and rt.fake.opened == []


async def test_busy_when_global_slots_are_full(rt, settings, monkeypatch):
    monkeypatch.setattr(settings, "machine_global_wait_s", 0.2)
    ids = [await _task(93_100 + i, f"t{i}") for i in range(3)]
    await rt.session(*ids[0])
    await rt.session(*ids[1])
    with pytest.raises(MachineBusy):
        await rt.session(*ids[2])


async def test_cancel_stops_open_sessions_and_releases_slots(rt):
    uid, tid = await _task(93_007, "long job")
    s = await rt.session(uid, tid)
    await rt.cancel(tid)
    assert s.id in rt.fake.stopped
    assert [r.status for r in await repo.sessions_for_task(tid, status=None)] == ["stopped"]
    assert tid not in await rt.slots.held()


async def test_release_meters_and_is_idempotent(rt):
    uid, tid = await _task(93_008, "metered")
    await rt.session(uid, tid)
    await rt.release(tid)
    await rt.release(tid)
    [row] = await repo.sessions_for_task(tid, status=None)
    assert row.status == "closed" and row.est_cost_usd >= 0


async def test_reaper_stops_sessions_of_finished_tasks(rt):
    from mavis.domain.tasks import TaskStatus

    uid, tid = await _task(93_009, "abandoned")
    s = await rt.session(uid, tid)
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.FAILED)
    rt._sessions.clear()  # simulate a worker restart: only the row survives
    assert await rt.reap() == 1
    assert s.id in rt.fake.stopped


async def test_exec_timeout_is_clamped(rt, settings):
    uid, tid = await _task(93_010, "slow")
    await rt.exec(uid, tid, ExecRequest(language="python", code="...", timeout_s=10_000))
    [session] = rt.fake.sessions.values()
    assert session.execs[0].timeout_s == settings.sandbox_exec_max_s


async def test_drive_timeout_releases_sessions_and_finalizes_card(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch, rt
):
    import asyncio

    from mavis import machine
    from mavis.agents import orchestrator
    from mavis.agents import orchestrator_graph as og
    from mavis.config import get_settings
    from mavis.domain.plans import Plan, PlanStep
    from mavis.domain.tasks import StepOutcome

    machine.set_runtime(rt)
    monkeypatch.setattr(get_settings(), "task_timeout_s", 0.3)

    async def _step(step, user_id, context):
        from mavis.tools.registry import current_task_id

        await rt.session(user_id, current_task_id.get())
        await asyncio.sleep(2)
        return StepOutcome(ok=True, text="late")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="i")]))
    tid = await tasks.create(user.id, goal="crunch a big file")
    await orchestrator.run_task(tid)
    [row] = await repo.sessions_for_task(tid, status=None)
    assert row.status == "closed"
    machine.set_runtime(None)


def test_machine_off_wires_nothing(settings):
    from mavis import machine
    from mavis.machine.wiring import register_machine

    register_machine()
    assert machine.get_runtime() is None


async def test_second_task_of_the_same_user_waits_its_turn(rt):
    from mavis.machine.quota import CONCURRENT_TEXT

    uid, t1 = await _task(93_020, "first")
    _, t2 = await _task(93_020, "second")
    await rt.session(uid, t1)
    with pytest.raises(QuotaExceeded) as info:
        await rt.session(uid, t2)
    assert info.value.user_text == CONCURRENT_TEXT
    await rt.release(t1)
    assert await rt.session(uid, t2)


@pytest.mark.parametrize("name,payload", [("a.csv", b"1,2"), ("deck.pptx", b"PK\x03"), ("note.md", b"# hi")])
async def test_our_own_writes_are_not_reported_as_made_by_code(rt, delivered, name, payload):
    uid, tid = await _task(93_030 + len(name), "attach and run")
    await rt.write_in(uid, tid, f"out/{name}", payload, provenance=Provenance.MAVIS)
    res = await rt.exec(uid, tid, ExecRequest(language="shell", code="true", timeout_s=5))
    assert res.changed == [] and delivered == []


async def test_a_full_workspace_does_not_lose_the_exec_result(rt, settings, monkeypatch):
    monkeypatch.setattr(settings, "workspace_quota_mb", 1)
    uid, tid = await _task(93_040, "big output")
    rt.fake.on_exec = _writes({"work/huge.bin": b"x" * (2 * 1024 * 1024)})
    res = await rt.exec(uid, tid, ExecRequest(language="python", code="...", timeout_s=5))
    assert res.ok and res.changed == [] and "storage is full" in res.stderr and "work/huge.bin" in res.stderr


async def test_files_over_the_send_limit_are_not_stored(rt, settings, monkeypatch, delivered):
    monkeypatch.setattr(settings, "machine_file_max_mb", 1)
    uid, tid = await _task(93_050, "too big to send")
    rt.fake.on_exec = _writes({"out/big.bin": b"x" * (2 * 1024 * 1024), "out/ok.txt": b"fine"})
    await rt.exec(uid, tid, ExecRequest(language="python", code="...", timeout_s=5))
    assert [p.path for p in await rt.store.list(uid)] == ["out/ok.txt"]
    assert len(delivered) == 1


async def test_release_hands_over_files_made_after_the_last_exec(rt, delivered):
    from mavis.domain.tasks import TaskStatus

    uid, tid = await _task(93_060, "ends badly")
    s = await rt.session(uid, tid)
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.FAILED)
    await s.write("out/partial.txt", b"half")
    await rt.release(tid)
    assert [d[:2] for d in delivered] == [(uid, tid)]


async def test_release_after_cancel_delivers_nothing(rt, delivered):
    uid, tid = await _task(93_070, "cancelled")
    s = await rt.session(uid, tid)
    await s.write("out/late.txt", b"late")
    await rt.cancel(tid)
    await rt.release(tid)
    assert delivered == []


async def test_cancel_during_an_exec_surfaces_as_an_error_not_a_hang(rt):
    uid, tid = await _task(93_080, "long job")
    await rt.session(uid, tid)
    await rt.cancel(tid)
    with pytest.raises(ConnectionError):
        [session] = rt.fake.sessions.values()
        await session.exec(ExecRequest(language="shell", code="x", timeout_s=5))


async def test_reaper_books_the_time_an_orphan_ran(rt):
    from sqlalchemy import select

    from mavis.domain.tasks import TaskStatus
    from mavis.store.db import Session
    from mavis.store.models import ComputeUsage

    uid, tid = await _task(93_090, "orphan")
    await rt.session(uid, tid)
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.FAILED)
    rt._sessions.clear()
    await rt.reap()
    async with Session() as s:
        rows = list(await s.scalars(select(ComputeUsage).where(ComputeUsage.user_id == uid)))
    assert len(rows) == 1 and rows[0].provider == "fake"


async def test_stop_all_stops_every_open_session(rt):
    ids = [await _task(93_200 + i, f"t{i}") for i in range(2)]
    for u, t in ids:
        await rt.session(u, t)
    assert await rt.stop_all() == 2
    assert len(rt.fake.stopped) == 2 and await repo.open_sessions() == []


def test_machine_on_wires_runtime_hook_and_reaper(settings, monkeypatch):
    from mavis import machine
    from mavis.agents import cancellation
    from mavis.machine import wiring

    monkeypatch.setenv("MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_BACKEND", "fake")
    from mavis.config import get_settings

    get_settings.cache_clear()
    cancellation.reset_for_tests()
    wiring.register_machine()
    wiring.register_machine()  # idempotent
    assert machine.get_runtime() is not None and machine.get_runtime().sandbox.name == "fake"
    assert cancellation._hooks.count(wiring._cancel_hook) == 1
    cancellation.reset_for_tests()
