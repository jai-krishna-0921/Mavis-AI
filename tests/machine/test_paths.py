from __future__ import annotations

import pytest

from mavis.machine.errors import SandboxPathError
from mavis.machine.paths import SAFE_ENV_KEYS, clip, guard, is_hidden, safe_env, safe_name
from mavis.machine.ports import FileClass, Provenance, class_of


@pytest.mark.parametrize("raw,clean", [("out/a.png", "out/a.png"), ("./work//x.csv", "work/x.csv"),
                                       ("inbox/My File.pdf", "inbox/My File.pdf")])
def test_guard_normalises(raw, clean):
    assert guard(raw) == clean


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "out/../../x", "", "a\\b", "a\x00b", "C:/x", "~/x"])
def test_guard_refuses(bad):
    with pytest.raises(SandboxPathError):
        guard(bad)


def test_hidden_and_classes():
    assert is_hidden(".mavis/builders/x.py") and not is_hidden("out/x.py")
    assert [class_of(p) for p in ("inbox/a", "out/b", "work/c", ".mavis/d", "e.txt")] == [
        FileClass.INBOX, FileClass.OUT, FileClass.WORK, FileClass.META, FileClass.WORK]


def test_provenance_trust():
    assert {p for p in Provenance if p.untrusted} == {Provenance.USER_UPLOAD, Provenance.FETCHED,
                                                       Provenance.GENERATED_TAINTED}


def test_safe_env_has_only_whitelisted_keys(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    monkeypatch.setenv("OLLAMA_API_KEY", "nope")
    env = safe_env("/tmp/ws")
    assert set(env) <= SAFE_ENV_KEYS and "nope" not in env.values() and env["HOME"] == "/tmp/ws"


@pytest.mark.parametrize("n", [10, 100, 5000])
def test_clip_keeps_the_tail(n):
    text = "".join(str(i % 10) for i in range(n))
    out = clip(text, 50)
    assert len(out) <= 50 + 40 and out.endswith(text[-20:])


@pytest.mark.parametrize("raw,clean", [("../../evil name!.csv", "evil_name.csv"), ("...", "file"),
                                       ("Résumé 2026.docx", "R_sum_2026.docx")])
def test_safe_name(raw, clean):
    assert safe_name(raw) == clean


async def test_local_symlink_escape_is_refused(tmp_path):
    from mavis.machine.local import LocalSandbox
    from mavis.machine.ports import ExecRequest

    sb = LocalSandbox(root=tmp_path / "r")
    s = await sb.open(user_id=1, task_id=1, timeout_s=60)
    try:
        await s.exec(ExecRequest(language="shell", code="mkdir -p out && ln -s /etc out/etc", timeout_s=10))
        with pytest.raises(SandboxPathError):
            await s.read("out/etc/hostname")
        with pytest.raises(SandboxPathError):
            await s.write("out/etc/planted", b"x")
    finally:
        await s.close()
