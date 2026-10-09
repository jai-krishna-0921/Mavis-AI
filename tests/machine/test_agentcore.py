"""AgentCoreSandbox against a fake boto client: mapping, timeouts, throttling, isolation."""

from __future__ import annotations

import json
import time

import pytest

from mavis.machine.agentcore import AgentCoreSandbox
from mavis.machine.errors import MachineBusy, SandboxPathError
from mavis.machine.ports import ExecRequest


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeAC:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, bytes]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.stopped: list[str] = []
        self.throttle = 0
        self.slow_s = 0.0
        self.next_exit = 0

    def start_code_interpreter_session(self, **kw):
        sid = f"s{len(self.sessions) + 1}"
        self.sessions[sid] = {}
        self.calls.append(("start", kw))
        return {"sessionId": sid}

    def stop_code_interpreter_session(self, **kw):
        self.stopped.append(kw["sessionId"])
        return {}

    def _result(self, stdout="", stderr="", code=0, content=None):
        return {
            "stream": [
                {
                    "result": {
                        "content": content or [],
                        "isError": code != 0,
                        "structuredContent": {"stdout": stdout, "stderr": stderr, "exitCode": code},
                    }
                }
            ]
        }

    def invoke_code_interpreter(self, **kw):
        self.calls.append((kw["name"], kw))
        if self.throttle:
            self.throttle -= 1
            raise ClientError("ThrottlingException")
        if kw["sessionId"] in self.stopped:
            raise ClientError("ResourceNotFoundException")
        files, args = self.sessions[kw["sessionId"]], kw["arguments"]
        if kw["name"] == "writeFiles":
            for item in args["content"]:
                files[item["path"]] = item["blob"] if "blob" in item else item["text"].encode()
            return self._result()
        if kw["name"] == "readFiles":
            p = args["paths"][0]
            if p not in files:
                return self._result(stderr="not found", code=1)
            return self._result(content=[{"type": "resource", "resource": {"blob": files[p]}}])
        if kw["name"] == "removeFiles":
            for p in args["paths"]:
                files.pop(p, None)
            return self._result()
        if kw["name"] == "executeCode" and "MAVIS_LIST" in args["code"]:
            import hashlib

            rows = [
                {"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for p, b in files.items()
            ]
            return self._result(stdout=json.dumps(rows))
        time.sleep(self.slow_s)
        source = args.get("code") or files.get(".mavis/tmp/run.py", b"").decode()
        return self._result(stdout="42\n" if "6 * 7" in source else "", code=self.next_exit)


@pytest.fixture
def ac(settings):
    fake = FakeAC()
    return AgentCoreSandbox(client=fake, identifier="aws.codeinterpreter.v1", region="ap-south-1"), fake


async def test_open_names_the_session_by_user_and_task(ac):
    sb, fake = ac
    s = await sb.open(user_id=17, task_id=230, timeout_s=900)
    assert fake.calls[0][1]["name"] == "u17-t230" and fake.calls[0][1]["sessionTimeoutSeconds"] == 900
    await s.close()
    assert fake.stopped == [s.id]


async def test_exec_maps_python_and_shell(ac):
    sb, fake = ac
    s = await sb.open(user_id=1, task_id=1, timeout_s=60)
    res = await s.exec(ExecRequest(language="python", code="print(6 * 7)", timeout_s=30))
    assert res.ok and res.stdout.strip() == "42" and res.exit_code == 0
    await s.exec(ExecRequest(language="shell", code="ls", timeout_s=30))
    names = [c[0] for c in fake.calls]
    assert "executeCommand" in names
    commands = [c[1]["arguments"]["command"] for c in fake.calls if c[0] == "executeCommand"]
    assert "python3 .mavis/tmp/run.py" in commands and "ls" in commands
    assert fake.sessions[s.id][".mavis/tmp/run.py"] == b"print(6 * 7)"


async def test_open_creates_the_workspace_folders(ac):
    sb, fake = ac
    await sb.open(user_id=1, task_id=9, timeout_s=60)
    first = next(c[1]["arguments"]["command"] for c in fake.calls if c[0] == "executeCommand")
    assert first.startswith("mkdir -p ") and all(d in first for d in ("inbox", "out", "work"))


@pytest.mark.parametrize("exit_code", [1, 2, 127])
async def test_nonzero_exit_is_reported(ac, exit_code):
    sb, fake = ac
    fake.next_exit = exit_code
    s = await sb.open(user_id=1, task_id=2, timeout_s=60)
    res = await s.exec(ExecRequest(language="python", code="x", timeout_s=30))
    assert not res.ok and res.exit_code == exit_code


async def test_files_round_trip_and_list(ac):
    sb, _ = ac
    s = await sb.open(user_id=2, task_id=3, timeout_s=60)
    await s.write("out/a.csv", b"a,b\n")
    assert await s.read("out/a.csv") == b"a,b\n"
    assert [e.path for e in await s.list()] == ["out/a.csv"]
    with pytest.raises(SandboxPathError):
        await s.write("../x", b"y")


async def test_timeout_stops_the_session(ac):
    sb, fake = ac
    fake.slow_s = 2.0
    s = await sb.open(user_id=3, task_id=4, timeout_s=60)
    res = await s.exec(ExecRequest(language="python", code="loop", timeout_s=1))
    assert res.timed_out and s.id in fake.stopped
    again = await s.exec(ExecRequest(language="python", code="print(1)", timeout_s=5))
    assert not again.ok and "stopped" in (again.error or "")


async def test_throttling_retries_once_then_is_busy(ac, monkeypatch):
    from mavis.machine import agentcore

    monkeypatch.setattr(agentcore, "RETRY_DELAY_S", 0.01)
    sb, fake = ac
    s = await sb.open(user_id=4, task_id=5, timeout_s=60)
    fake.throttle = 1
    assert (await s.exec(ExecRequest(language="python", code="print(6 * 7)", timeout_s=30))).ok
    fake.throttle = 2
    with pytest.raises(MachineBusy):
        await s.exec(ExecRequest(language="python", code="print(6 * 7)", timeout_s=30))


async def test_selection_refuses_local_in_prod_without_credentials(settings, monkeypatch):
    from mavis.machine import selection

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(selection, "_aws_credentials", lambda: False)
    with pytest.raises(RuntimeError):
        selection.build_sandbox()


async def test_selection_picks_agentcore_when_credentials_resolve(settings, monkeypatch):
    from mavis.machine import selection

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(selection, "_aws_credentials", lambda: True)
    monkeypatch.setattr("mavis.machine.agentcore.AgentCoreSandbox.__init__", lambda self, **kw: None)
    assert type(selection.build_sandbox()).__name__ == "AgentCoreSandbox"


async def test_selection_refuses_explicit_local_in_prod(settings, monkeypatch):
    from mavis.machine import selection

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "sandbox_backend", "local")
    with pytest.raises(RuntimeError, match="refused in prod"):
        selection.build_sandbox()


async def test_selection_auto_in_dev_without_credentials_is_local(settings, monkeypatch):
    from mavis.machine import selection

    monkeypatch.setattr(settings, "env", "dev")
    monkeypatch.setattr(settings, "sandbox_backend", "auto")
    monkeypatch.setattr(selection, "_aws_credentials", lambda: False)
    assert selection.build_sandbox().name == "local"
