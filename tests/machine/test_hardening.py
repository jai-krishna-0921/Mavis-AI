"""Security review fixes: store and session reconcile, one size limit, one provenance, odd names, timeouts,
atomic limits, isolation gate, package allowlist and purge."""

from __future__ import annotations

import asyncio

import pytest

from mavis import machine
from mavis.machine.errors import QuotaExceeded
from mavis.machine.fake import FakeSandbox, MemoryWorkspaceStore
from mavis.machine.ports import ExecRequest, ExecResult, Provenance
from mavis.machine.quota import GlobalSlots
from mavis.machine.runtime import MachineRuntime
from mavis.store.repo import machine as repo
from mavis.store.repo import tasks, users
from mavis.tools.registry import ToolRun, current_run


@pytest.fixture
async def rt(db, settings, monkeypatch):
    from mavis.machine import quota

    monkeypatch.setattr(quota, "get_redis", lambda: None)
    delivered = []

    async def deliver(user_id, task_id, artifact_id, proactive=False, **_kw):
        delivered.append(artifact_id)
        return True

    runtime = MachineRuntime(FakeSandbox(), MemoryWorkspaceStore(), slots=GlobalSlots(size=3),
                             deliver=deliver)
    runtime.delivered = delivered
    return runtime


async def _task(chat: int, goal: str = "g"):
    u, _ = await users.get_or_create_by_chat(chat, "R")
    return u.id, await tasks.create(u.id, goal=goal)


def _req(code="x"):
    return ExecRequest(language="python", code=code, timeout_s=30)


def _put(rt, uid, path, data=b"x", prov=Provenance.GENERATED_CLEAN):
    return rt.store.put(uid, path, data, provenance=prov, cls=None)


# --- 1: expiry --------------------------------------------------------------------------------
async def test_expired_object_marks_its_row_deleted_and_the_session_still_opens(rt):
    uid, tid = await _task(94_001)
    await _put(rt, uid, "work/old.csv")
    await _put(rt, uid, "work/kept.csv")
    rt.store.blobs.pop((uid, "work/old.csv"))  # the bucket lifecycle took it
    res = await rt.exec(uid, tid, _req())
    assert "work/old.csv" in res.stderr
    assert await rt.store.meta(uid, "work/old.csv") is None
    [session] = rt.sandbox.sessions.values()
    assert "work/kept.csv" in session.files and "work/old.csv" not in session.files


async def test_failed_sync_in_closes_the_sandbox_and_registers_nothing(rt):
    uid, tid = await _task(94_002)
    await _put(rt, uid, "work/a.csv")

    async def boom(user_id, path):
        raise RuntimeError("s3 down")

    rt.store.get = boom
    with pytest.raises(RuntimeError):
        await rt.session(uid, tid)
    [session] = rt.sandbox.sessions.values()
    assert session.closed
    assert await repo.sessions_for_task(tid) == []
    assert await rt.slots.held() == []
    assert tid not in rt._sessions


async def test_row_registration_failure_does_not_leak_a_billed_session(rt, monkeypatch):
    uid, tid = await _task(94_003)

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "bind_session", boom)
    with pytest.raises(RuntimeError):
        await rt.session(uid, tid)
    [session] = rt.sandbox.sessions.values()
    assert session.closed and await repo.sessions_for_task(tid) == []


# --- 2: one size limit ------------------------------------------------------------------------
@pytest.fixture
def small_cap(settings, monkeypatch):
    monkeypatch.setattr(settings, "machine_file_max_mb", 50)
    monkeypatch.setattr(settings, "agentcore_write_max_mb", 1)
    assert settings.file_max_bytes == 1024 * 1024


async def test_stored_files_never_exceed_what_can_be_written_back(rt, small_cap):
    uid, _tid = await _task(94_004)
    with pytest.raises(QuotaExceeded):
        await _put(rt, uid, "work/huge.bin", b"x" * (1024 * 1024 + 1))


async def test_big_files_and_wheels_are_kept_but_not_synced_in(rt, small_cap, settings):
    uid, tid = await _task(94_005)
    await _put(rt, uid, "work/ok.txt")
    await _put(rt, uid, ".mavis/wheels/w-1-py3-none-any.whl", b"PK")
    await _put(rt, uid, "work/big.bin", b"x" * 100)
    # a row that is bigger than the limit (stored before the limit existed)
    from sqlalchemy import update

    from mavis.store.db import Session
    from mavis.store.models import WorkspaceFileRow

    async with Session() as s:
        await s.execute(update(WorkspaceFileRow).where(WorkspaceFileRow.path == "work/big.bin")
                        .values(size=2 * 1024 * 1024))
        await s.commit()
    res = await rt.exec(uid, tid, _req())
    [session] = rt.sandbox.sessions.values()
    assert set(session.files) == {"work/ok.txt"}
    assert "work/big.bin" in res.stderr
    assert await rt.store.meta(uid, "work/big.bin") is not None  # kept


async def test_listing_comes_from_the_store_so_delivery_is_idempotent(rt):
    uid, tid = await _task(94_006)
    await _put(rt, uid, "out/report.txt", b"r")
    await rt.exec(uid, tid, _req())
    assert rt.delivered == []  # already delivered in an earlier task: not sent again
    assert (await rt.session(uid, tid)) is not None


# --- 3: one provenance ------------------------------------------------------------------------
async def test_tainted_turn_writes_tainted_files_via_exec_and_builders(rt):
    uid, tid = await _task(94_007)

    def on_exec(req, files):
        files["work/derived.csv"] = b"1"
        files["out/t.xlsx"] = b"PK"
        return ExecResult(ok=True, exit_code=0)

    rt.sandbox.on_exec = on_exec
    token = current_run.set(ToolRun(tainted=True))
    try:
        await rt.exec(uid, tid, _req())
        await rt.build(uid, tid, "xlsx", {"sheets": []}, "t.xlsx")
    finally:
        current_run.reset(token)
    assert (await rt.store.meta(uid, "work/derived.csv")).provenance is Provenance.GENERATED_TAINTED
    assert (await rt.store.meta(uid, "out/t.xlsx")).provenance is Provenance.GENERATED_TAINTED
    # a later task that reads the file is tainted
    await rt.release(tid)
    later = await tasks.create(uid, goal="later")
    rt.sandbox.on_exec = None
    await rt.exec(uid, later, _req())
    assert rt.untrusted(later)


async def test_clean_turn_stays_clean(rt):
    uid, tid = await _task(94_008)
    rt.sandbox.on_exec = lambda req, files: (files.update({"work/c.csv": b"1"}),
                                             ExecResult(ok=True, exit_code=0))[1]
    await rt.exec(uid, tid, _req())
    assert (await rt.store.meta(uid, "work/c.csv")).provenance is Provenance.GENERATED_CLEAN


# --- 4: odd names -----------------------------------------------------------------------------
async def test_unrepresentable_names_are_skipped_and_reported_once(rt):
    uid, tid = await _task(94_009)

    def on_exec(req, files):
        files.update({"a\\b.txt": b"1", "~tmp": b"2", "C:x.txt": b"3", "out/ok.txt": b"4"})
        return ExecResult(ok=True, exit_code=0)

    rt.sandbox.on_exec = on_exec
    first = await rt.exec(uid, tid, _req())
    assert [c.path for c in first.changed] == ["out/ok.txt"]
    assert "cannot keep" in first.stderr
    second = await rt.exec(uid, tid, _req())
    assert "cannot keep" not in second.stderr and second.error is None


# --- 5: timeout -------------------------------------------------------------------------------
async def test_timeout_returns_the_result_and_the_next_call_gets_a_fresh_machine(rt):
    uid, tid = await _task(94_010)
    await _put(rt, uid, "work/saved.txt")
    rt.sandbox.results = [ExecResult(ok=False, timed_out=True, error="timed out after 30s")]
    res = await rt.exec(uid, tid, _req())
    assert res.timed_out and "fresh machine" in res.stderr
    assert len(rt.sandbox.opened) == 1
    again = await rt.exec(uid, tid, _req())
    assert again.ok and len(rt.sandbox.opened) == 2
    assert "work/saved.txt" in list(rt.sandbox.sessions.values())[-1].files
    [row] = await repo.sessions_for_task(tid)
    assert row.session_id == list(rt.sandbox.sessions)[-1]


# --- 9: atomic per-user limit -----------------------------------------------------------------
async def test_concurrent_tasks_of_one_user_cannot_both_open(rt):
    uid, t1 = await _task(94_011)
    t2 = await tasks.create(uid, goal="second")
    out = await asyncio.gather(rt.session(uid, t1), rt.session(uid, t2), return_exceptions=True)
    assert sum(isinstance(o, QuotaExceeded) for o in out) == 1
    assert len(rt.sandbox.opened) == 1


# --- intake -----------------------------------------------------------------------------------
async def test_intake_tells_the_user_when_storage_is_full(db, settings, monkeypatch):
    from mavis.channels import set_channel
    from mavis.channels.fake import FakeChannel
    from mavis.machine import intake
    from mavis.store.repo import outbox
    from tests.machine.test_intake import _ev

    monkeypatch.setattr(settings, "machine_enabled", True)
    runtime = MachineRuntime(FakeSandbox(), MemoryWorkspaceStore())
    machine.set_runtime(runtime)
    set_channel(FakeChannel())
    sent = []

    async def enqueue_now(out):
        sent.append(out.text)

    monkeypatch.setattr(outbox, "enqueue_now", enqueue_now)
    monkeypatch.setattr(settings, "workspace_quota_mb", 0)
    u, _ = await users.get_or_create_by_chat(94_012, "R")
    try:
        await intake.on_user_message(_ev(u.id, "a.csv"))
    finally:
        set_channel(None)
        machine.set_runtime(None)
    assert any("full" in t for t in sent)


# --- 7: isolation gate ------------------------------------------------------------------------
async def test_isolation_check_fails_closed_and_is_cached():
    from mavis.machine import isolation

    isolation.reset()
    sb = FakeSandbox()
    sb.on_exec = lambda req, files: ExecResult(ok=True, exit_code=0, stdout="OPEN\n")
    assert not (await isolation.check_isolation(sb)).isolated
    sb.on_exec = lambda req, files: ExecResult(ok=True, exit_code=0, stdout="BLOCKED\n")
    assert not (await isolation.check_isolation(sb)).isolated  # cached
    assert (await isolation.check_isolation(sb, force=True)).isolated
    isolation.reset()
    sb.fail_open = RuntimeError("x")
    assert not (await isolation.check_isolation(sb)).isolated
    isolation.reset()


@pytest.mark.parametrize("stdout,registered", [("BLOCKED\n", True), ("OPEN\n", False)])
async def test_prod_registers_tools_only_after_a_passing_check(settings, monkeypatch, stdout, registered):
    from mavis.machine import isolation, wiring
    from mavis.tools.registry import ToolRegistry

    isolation.reset()
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "machine_enabled", True)
    sb = FakeSandbox()
    sb.on_exec = lambda req, files: ExecResult(ok=True, exit_code=0, stdout=stdout)
    machine.set_runtime(MachineRuntime(sb, MemoryWorkspaceStore()))
    reg = ToolRegistry()
    monkeypatch.setattr("mavis.tools.registry.get_registry", lambda: reg)
    monkeypatch.setattr(wiring, "_start_reaper", lambda: asyncio.sleep(0))
    monkeypatch.setattr(wiring, "_activate", lambda: __import__(
        "mavis.tools.machine_tools", fromlist=["x"]).register_machine_tools(reg))
    try:
        await wiring._gate_on_isolation()
        assert (reg.find("machine_run_python") is not None) is registered
        assert (machine.get_runtime() is not None) is registered
    finally:
        machine.set_runtime(None)
        isolation.reset()


def test_prod_with_egress_allowed_registers_without_a_check(settings, monkeypatch):
    from mavis.machine import wiring

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "machine_enabled", True)
    monkeypatch.setattr(settings, "machine_allow_egress", True)
    machine.set_runtime(MachineRuntime(FakeSandbox(), MemoryWorkspaceStore()))
    called = []
    monkeypatch.setattr(wiring, "_activate", lambda: called.append(1))
    try:
        wiring.register_machine()
    finally:
        machine.set_runtime(None)
    assert called == [1]


# --- 8: wheels --------------------------------------------------------------------------------
async def test_default_allowlist_applies_when_empty_and_blocks_others(settings, monkeypatch, tmp_path):
    from mavis.machine.wheels import DEFAULT_ALLOW, WheelCache

    monkeypatch.setattr(settings, "machine_package_allow", [])
    assert "pandas" in DEFAULT_ALLOW and "requests" not in DEFAULT_ALLOW
    with pytest.raises(ValueError, match="allowlist"):
        await WheelCache(cache_dir=tmp_path).resolve(["requests"])


async def test_cached_wheels_are_rehashed_before_use(tmp_path):
    import hashlib

    import httpx

    from mavis.machine.wheels import WheelCache

    good = b"PKgood"
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=good)

    cache = WheelCache(cache_dir=tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sha = hashlib.sha256(good).hexdigest()
        assert await cache._fetch(client, "x-1-py3-none-any.whl", "https://f/x", sha) == good
        assert await cache._fetch(client, "x-1-py3-none-any.whl", "https://f/x", sha) == good
        assert len(calls) == 1
        (tmp_path / "x-1-py3-none-any.whl").write_bytes(b"PKevil")  # tampered on disk
        assert await cache._fetch(client, "x-1-py3-none-any.whl", "https://f/x", sha) == good
        assert len(calls) == 2


# --- 9: purge ---------------------------------------------------------------------------------
async def test_purge_retries_failed_deletes_and_keeps_rows_of_survivors(db):
    from mavis.machine.s3store import S3WorkspaceStore
    from tests.machine.test_workspace_store import FakeS3

    class Flaky(FakeS3):
        def __init__(self, stuck):
            super().__init__()
            self.stuck, self.attempts = stuck, 0

        def delete_objects(self, *, Bucket, Delete):  # noqa: N803
            errors = []
            for o in Delete["Objects"]:
                if o["Key"] in self.stuck:
                    self.attempts += 1
                    errors.append({"Key": o["Key"], "Code": "InternalError"})
                else:
                    self.objects.pop(o["Key"], None)
            return {"Errors": errors}

    u, _ = await users.get_or_create_by_chat(94_020, "R")
    s3 = Flaky(set())
    st = S3WorkspaceStore(bucket="b", prefix="ws/", client=s3)
    for p in ("out/a.txt", "out/b.txt"):
        await st.put(u.id, p, b"x", provenance=Provenance.GENERATED_CLEAN, cls=None)
    s3.stuck = {f"ws/u{u.id}/out/b.txt"}
    with pytest.raises(RuntimeError):
        await st.purge_user(u.id)
    assert s3.attempts == 3
    assert [f.path for f in await st.list(u.id)] == ["out/b.txt"]
    s3.stuck = set()
    await st.purge_user(u.id)
    assert await st.list(u.id) == []
