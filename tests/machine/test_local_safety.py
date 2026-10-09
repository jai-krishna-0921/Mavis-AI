"""LocalSandbox is dev-only but must still be safe to run on a developer machine."""

from __future__ import annotations

import pytest

from mavis.machine.local import LocalSandbox
from mavis.machine.ports import ExecRequest


@pytest.fixture
async def sess(tmp_path, settings):
    s = await LocalSandbox(root=tmp_path / "r").open(user_id=1, task_id=1, timeout_s=60)
    yield s
    await s.close()


@pytest.mark.parametrize("target", [("127.0.0.1", 9), ("example.org", 80), ("10.255.255.1", 443)])
async def test_no_network_by_default(sess, target):
    code = f"import socket\ns = socket.socket()\ns.settimeout(2)\ns.connect({target!r})\nprint('connected')"
    res = await sess.exec(ExecRequest(language="python", code=code, timeout_s=20))
    assert not res.ok and "connected" not in res.stdout


async def test_file_size_limit(sess, settings, monkeypatch):
    monkeypatch.setattr(settings, "local_sandbox_file_mb", 1)
    code = "open('work/big.bin','wb').write(b'x' * (5 * 1024 * 1024))"
    res = await sess.exec(ExecRequest(language="python", code=code, timeout_s=20))
    assert not res.ok


async def test_memory_limit(sess, settings, monkeypatch):
    monkeypatch.setattr(settings, "local_sandbox_mem_mb", 256)
    code = "x = bytearray(900 * 1024 * 1024)"
    res = await sess.exec(ExecRequest(language="python", code=code, timeout_s=20))
    assert not res.ok


async def test_output_flood_is_stopped(sess, settings, monkeypatch):
    monkeypatch.setattr(settings, "machine_stdout_max_chars", 1000)
    res = await sess.exec(ExecRequest(language="shell", code="yes", timeout_s=20))
    assert not res.ok and len(res.stdout) < 5000 and not res.timed_out


async def test_fresh_workspace_has_the_standard_folders(sess):
    res = await sess.exec(ExecRequest(language="shell", code="ls -d inbox out work", timeout_s=10))
    assert res.ok and res.stdout.split() == ["inbox", "out", "work"]


async def test_close_kills_background_children(tmp_path, settings):
    sb = LocalSandbox(root=tmp_path / "r2")
    s = await sb.open(user_id=2, task_id=2, timeout_s=60)
    await s.exec(ExecRequest(language="shell", code="sleep 300 &\necho started", timeout_s=10))
    await s.close()
    await s.close()
