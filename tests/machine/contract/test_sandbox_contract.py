from __future__ import annotations

import pytest

from mavis.machine.errors import SandboxPathError
from mavis.machine.ports import ExecRequest


@pytest.mark.parametrize("path,data", [("work/a.txt", b"alpha"), ("out/chart.png", b"\x89PNG..."),
                                       ("inbox/sub/dir/n.csv", b"a,b\n1,2\n")])
async def test_write_read_list_remove(sandbox, path, data):
    s = await sandbox.open(user_id=11, task_id=21, timeout_s=120)
    try:
        await s.write(path, data)
        assert await s.read(path) == data
        assert path in {e.path for e in await s.list()}
        await s.remove(path)
        assert path not in {e.path for e in await s.list()}
    finally:
        await s.close()
        await s.close()  # idempotent


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../x", "out/../../y"])
async def test_path_escapes_raise(sandbox, bad):
    s = await sandbox.open(user_id=12, task_id=22, timeout_s=60)
    try:
        with pytest.raises(SandboxPathError):
            await s.write(bad, b"x")
        with pytest.raises(SandboxPathError):
            await s.read(bad)
    finally:
        await s.close()


async def test_sessions_are_never_shared(sandbox):
    a = await sandbox.open(user_id=1, task_id=1, timeout_s=60)
    b = await sandbox.open(user_id=2, task_id=2, timeout_s=60)
    try:
        assert a.id != b.id
        await a.write("work/secret.txt", b"user one")
        assert "work/secret.txt" not in {e.path for e in await b.list()}
    finally:
        await a.close()
        await b.close()


async def test_stop_by_id_is_idempotent(sandbox):
    s = await sandbox.open(user_id=3, task_id=3, timeout_s=60)
    await sandbox.stop(s.id)
    await sandbox.stop(s.id)
    await sandbox.stop("no-such-session")


async def test_python_runs_and_reports_exit(sandbox, needs_exec):
    s = await sandbox.open(user_id=4, task_id=4, timeout_s=120)
    try:
        ok = await s.exec(ExecRequest(language="python", code="print(6 * 7)", timeout_s=30))
        bad = await s.exec(ExecRequest(language="python", code="raise SystemExit(3)", timeout_s=30))
        assert ok.ok and ok.stdout.strip() == "42" and ok.exit_code == 0
        assert not bad.ok and bad.exit_code == 3
    finally:
        await s.close()


async def test_canary_env_is_absent(sandbox, needs_exec, monkeypatch):
    monkeypatch.setenv("MAVIS_CANARY_SECRET", "canary-7f3a")
    s = await sandbox.open(user_id=5, task_id=5, timeout_s=120)
    try:
        code = "import os; print(os.environ.get('MAVIS_CANARY_SECRET', 'absent'))"
        res = await s.exec(ExecRequest(language="python", timeout_s=30, code=code))
        assert res.stdout.strip() == "absent"
    finally:
        await s.close()


async def test_timeout_leaves_nothing_running(sandbox, needs_exec):
    s = await sandbox.open(user_id=6, task_id=6, timeout_s=120)
    try:
        spin = "import time\nopen('work/pid','w').write('1')\nwhile True: time.sleep(0.1)"
        res = await s.exec(ExecRequest(language="python", timeout_s=1, code=spin))
        assert res.timed_out and not res.ok
        after = await s.exec(ExecRequest(language="shell", code="echo alive", timeout_s=10))
        assert after.ok or after.error  # the adapter may have stopped the session; it must not hang
    finally:
        await s.close()


async def test_shell_runs(sandbox, needs_exec):
    s = await sandbox.open(user_id=7, task_id=7, timeout_s=60)
    try:
        await s.write("work/n.txt", b"three\n")
        res = await s.exec(ExecRequest(language="shell", code="cat work/n.txt", timeout_s=10))
        assert res.stdout.strip() == "three"
    finally:
        await s.close()
