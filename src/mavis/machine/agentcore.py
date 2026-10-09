"""AWS Bedrock AgentCore Code Interpreter adapter (spec 5.2). Only this module and the browser adapter
import boto3 for AgentCore. Credentials come from the instance role; the interpreter has no execution
role and (SANDBOX mode) no network, so nothing in the session can reach AWS or the internet."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import structlog

from mavis.config import get_settings
from mavis.machine.errors import MachineBusy
from mavis.machine.paths import WORK_DIRS, clip, guard
from mavis.machine.ports import BackendHealth, ExecRequest, ExecResult, FileEntry

log = structlog.get_logger(__name__)
BUSY_CODES = {"ThrottlingException", "ServiceQuotaExceededException", "TooManyRequestsException"}
RETRY_DELAY_S = 2.0
CALL_TIMEOUT_S = 60.0
SCRIPT_PATH = ".mavis/tmp/run.py"
UNREACHABLE = "my machine isn't reachable right now"
LIST_SCRIPT = """# MAVIS_LIST
import hashlib, json, os
rows = []
for base, dirs, files in os.walk('.'):
    for f in files:
        p = os.path.relpath(os.path.join(base, f), '.').replace(os.sep, '/')
        if p.startswith('.mavis/tmp/') or os.path.islink(p):
            continue
        with open(p, 'rb') as fh:
            data = fh.read()
        rows.append({'path': p, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
print(json.dumps(rows))
"""


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _parse(resp: dict) -> tuple[dict, list[dict], bool]:
    structured: dict = {}
    content: list[dict] = []
    is_error = False
    for event in resp.get("stream", []):
        result = event.get("result") or {}
        structured.update(result.get("structuredContent") or {})
        content += list(result.get("content") or [])
        is_error = is_error or bool(result.get("isError"))
    return structured, content, is_error


class AgentCoreSession:
    def __init__(self, owner: AgentCoreSandbox, session_id: str) -> None:
        self.id, self._o, self.dead = session_id, owner, False

    async def _invoke(self, name: str, arguments: dict, timeout_s: float = CALL_TIMEOUT_S) -> dict:
        for attempt in range(2):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self._o.client.invoke_code_interpreter,
                        codeInterpreterIdentifier=self._o.identifier,
                        sessionId=self.id,
                        name=name,
                        arguments=arguments,
                    ),
                    timeout=timeout_s,
                )
            except TimeoutError:
                raise
            except Exception as exc:  # noqa: BLE001 - boto ClientError and transport errors
                if _code(exc) in BUSY_CODES:
                    if attempt == 0:
                        await asyncio.sleep(RETRY_DELAY_S)
                        continue
                    raise MachineBusy("the machine is busy, retrying did not help") from exc
                raise
        raise MachineBusy("the machine is busy")

    async def exec(self, req: ExecRequest, on_output=None) -> ExecResult:
        if self.dead:
            return ExecResult(ok=False, error="the session was stopped after a timeout")
        try:
            if req.language == "python":
                # executeCode runs in a shared kernel that reports SystemExit as exit code 1; a script run
                # gives the real exit code and a fresh interpreter per call, like the local sandbox.
                await self._invoke("writeFiles", {"content": [{"path": SCRIPT_PATH, "text": req.code}]})
                resp = await self._invoke(
                    "executeCommand", {"command": f"python3 {SCRIPT_PATH}"}, timeout_s=req.timeout_s
                )
            else:
                resp = await self._invoke("executeCommand", {"command": req.code}, timeout_s=req.timeout_s)
        except TimeoutError:
            self.dead = True
            await self._o.stop(self.id)
            return ExecResult(ok=False, timed_out=True, error=f"timed out after {req.timeout_s}s")
        except MachineBusy:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("agentcore.exec_failed", error=type(exc).__name__, code=_code(exc))
            return ExecResult(ok=False, error=UNREACHABLE)
        out, _content, is_error = _parse(resp)
        limit = get_settings().machine_stdout_max_chars
        code = out.get("exitCode")
        return ExecResult(
            ok=(code == 0) if code is not None else not is_error,
            exit_code=code,
            stdout=clip(str(out.get("stdout", "")), limit),
            stderr=clip(str(out.get("stderr", "")), limit),
            duration_s=float(out.get("executionTime") or 0.0),
        )

    async def write(self, path: str, data: bytes) -> None:
        if len(data) > get_settings().agentcore_write_max_mb * 1024 * 1024:
            raise ValueError(f"{path} is too large to write into the machine")
        await self._invoke("writeFiles", {"content": [{"path": guard(path), "blob": data}]})

    async def read(self, path: str) -> bytes:
        _out, content, _is_error = _parse(await self._invoke("readFiles", {"paths": [guard(path)]}))
        for item in content:
            res = item.get("resource") or {}
            if "blob" in res:
                blob = res["blob"]
                return blob if isinstance(blob, bytes | bytearray) else base64.b64decode(blob)
            if "text" in res:
                return str(res["text"]).encode()
        raise FileNotFoundError(path)

    async def list(self, path: str = "") -> list[FileEntry]:
        prefix = guard(path) + "/" if path else ""
        out, _c, _e = _parse(await self._invoke("executeCode", {"code": LIST_SCRIPT, "language": "python"}))
        rows = json.loads(str(out.get("stdout") or "[]"))
        return sorted((FileEntry(**r) for r in rows if r["path"].startswith(prefix)), key=lambda e: e.path)

    async def remove(self, path: str) -> None:
        await self._invoke("removeFiles", {"paths": [guard(path)]})

    async def close(self) -> None:
        if not self.dead:
            self.dead = True
            await self._o.stop(self.id)


class AgentCoreSandbox:
    name = "agentcore"

    def __init__(self, client: Any = None, identifier: str | None = None, region: str | None = None) -> None:
        s = get_settings()
        self.identifier = identifier or s.agentcore_code_interpreter_id
        if client is None:
            import boto3

            client = boto3.client("bedrock-agentcore", region_name=region or s.agentcore_region)
        self.client = client

    async def open(self, *, user_id: int, task_id: int, timeout_s: int) -> AgentCoreSession:
        try:
            resp = await asyncio.to_thread(
                self.client.start_code_interpreter_session,
                codeInterpreterIdentifier=self.identifier,
                name=f"u{int(user_id)}-t{int(task_id)}",
                sessionTimeoutSeconds=int(timeout_s),
            )
        except Exception as exc:  # noqa: BLE001
            if _code(exc) in BUSY_CODES:
                raise MachineBusy("the machine is busy") from exc
            raise
        session = AgentCoreSession(self, resp["sessionId"])
        try:  # a fresh interpreter has none of the workspace folders the tools and prompts rely on
            await session._invoke("executeCommand", {"command": "mkdir -p " + " ".join(WORK_DIRS)})
        except BaseException:
            await session.close()
            raise
        return session

    async def stop(self, session_id: str) -> None:
        try:
            await asyncio.to_thread(
                self.client.stop_code_interpreter_session,
                codeInterpreterIdentifier=self.identifier,
                sessionId=session_id,
            )
        except Exception as exc:  # noqa: BLE001 - already stopped or expired is fine
            log.info("agentcore.stop_ignored", code=_code(exc))

    async def health(self) -> BackendHealth:
        try:
            s = await self.open(user_id=0, task_id=0, timeout_s=60)
            await s.close()
            return BackendHealth(ok=True)
        except Exception as exc:  # noqa: BLE001
            return BackendHealth(ok=False, detail=type(exc).__name__)
