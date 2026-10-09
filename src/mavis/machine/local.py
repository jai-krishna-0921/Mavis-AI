"""Dev-only sandbox: a subprocess in a temp workspace. NOT the production backend.

Safety for local use: empty whitelisted environment, per-command resource limits (CPU seconds, address
space, file size, no core dumps), no network (a fresh network namespace via `unshare -rn` when the host
allows it, otherwise a socket block injected into Python commands and a loud warning), the shared path
guard with symlink resolution, a hard output cap, and a process group that is killed on timeout, close
and stop. `auto` never picks it when ENV=prod."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import resource
import shutil
import signal
import sys
import time
from pathlib import Path

import structlog

from mavis.config import get_settings
from mavis.machine.errors import SandboxPathError
from mavis.machine.paths import WORK_DIRS, clip, guard, safe_env
from mavis.machine.ports import BackendHealth, ExecRequest, ExecResult, FileEntry
from mavis.machine.s3store import MetaReads, put_with_quota
from mavis.store.repo import machine as repo_machine

log = structlog.get_logger(__name__)
_ids = itertools.count(1)
_NO_SOCKETS = (
    "import socket as _s\n"
    "def _blocked(*a, **k):\n    raise OSError('network is disabled in this sandbox')\n"
    "_s.socket.connect = _s.socket.connect_ex = _s.socket.sendto = _blocked\n"
    "_s.getaddrinfo = _s.gethostbyname = _blocked\n"
    "del _s, _blocked\n"
)
_unshare_ok: bool | None = None


def _probe_unshare() -> bool:
    """True when a command can be started in a new, empty network namespace on this host."""
    global _unshare_ok
    if _unshare_ok is None:
        import subprocess

        exe = shutil.which("unshare")
        try:
            _unshare_ok = bool(exe) and subprocess.run([exe, "-rn", "true"], capture_output=True,
                                                       timeout=10).returncode == 0
        except Exception:  # noqa: BLE001
            _unshare_ok = False
    return _unshare_ok


def _limits(mem: int, cpu_s: int, file_b: int):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_b, file_b))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    return apply


class LocalSession:
    def __init__(self, root: Path, python: str) -> None:
        self.id = f"local-{os.getpid()}-{next(_ids)}"
        self.root, self._python = root, python
        for d in WORK_DIRS:
            (root / d).mkdir(parents=True, exist_ok=True)
        self._procs: set[asyncio.subprocess.Process] = set()
        self.closed = False

    def _host(self, path: str) -> Path:
        target = (self.root / guard(path)).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise SandboxPathError(f"path resolves outside the workspace: {path[:80]!r}")
        return target

    def _argv(self, req: ExecRequest) -> list[str]:
        s = get_settings()
        if req.language == "python":
            code = req.code if s.local_sandbox_network else _NO_SOCKETS + req.code
            argv = [self._python, "-I", "-c", code]
        else:
            argv = ["/bin/sh", "-c", req.code]
        if not s.local_sandbox_network and _probe_unshare():
            argv = [shutil.which("unshare") or "unshare", "-rn", *argv]
        return argv

    async def exec(self, req: ExecRequest, on_output=None) -> ExecResult:
        if self.closed:
            return ExecResult(ok=False, error="the session is closed")
        s = get_settings()
        cap = max(1, s.machine_stdout_max_chars) * 4
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(req), cwd=self.root, env=safe_env(str(self.root)),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
                preexec_fn=_limits(s.local_sandbox_mem_mb * 1024 * 1024, s.local_sandbox_cpu_s,
                                   s.local_sandbox_file_mb * 1024 * 1024))
        except OSError as exc:
            return ExecResult(ok=False, error=f"could not start the command: {type(exc).__name__}")
        self._procs.add(proc)
        flooded = False

        async def drain(stream) -> bytes:
            nonlocal flooded
            buf = bytearray()
            while chunk := await stream.read(65536):
                if flooded:
                    continue  # keep reading to EOF so the pipes close; the command is already killed
                buf += chunk
                if len(buf) > cap:
                    flooded = True
                    self._kill(proc)
                    del buf[:-cap]
            return bytes(buf)

        try:
            out, err = await asyncio.wait_for(asyncio.gather(drain(proc.stdout), drain(proc.stderr)),
                                              timeout=req.timeout_s)
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            self._kill(proc)
            await proc.wait()
            return ExecResult(ok=False, timed_out=True, error=f"timed out after {req.timeout_s}s",
                              duration_s=time.monotonic() - started)
        finally:
            self._procs.discard(proc)
            self._kill(proc)  # nothing the command spawned outlives it
        return ExecResult(ok=proc.returncode == 0 and not flooded, exit_code=proc.returncode,
                          stdout=clip(out.decode("utf-8", "replace"), s.machine_stdout_max_chars),
                          stderr=clip(err.decode("utf-8", "replace"), s.machine_stdout_max_chars),
                          error="output too large, the command was stopped" if flooded else None,
                          duration_s=time.monotonic() - started)

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    async def write(self, path: str, data: bytes) -> None:
        target = self._host(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._host(path)  # the parent may have been a symlink created just now: re-check
        target.write_bytes(data)

    async def read(self, path: str) -> bytes:
        return self._host(path).read_bytes()

    async def list(self, path: str = "") -> list[FileEntry]:
        base = self._host(path) if path else self.root
        out = []
        for p in sorted(base.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(self.root).as_posix()
            if rel.startswith(".mavis/tmp/"):
                continue
            data = p.read_bytes()
            out.append(FileEntry(path=rel, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                 mtime=p.stat().st_mtime))
        return out

    async def remove(self, path: str) -> None:
        self._host(path).unlink(missing_ok=True)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for proc in list(self._procs):
            self._kill(proc)
        shutil.rmtree(self.root, ignore_errors=True)


class LocalSandbox:
    name = "local"

    def __init__(self, root: Path | None = None, python: str | None = None) -> None:
        self._root = root or (get_settings().data_dir / "machine-local")
        self._python = python or sys.executable
        self._sessions: dict[str, LocalSession] = {}

    async def open(self, *, user_id: int, task_id: int, timeout_s: int) -> LocalSession:
        log.warning("sandbox.local_backend_in_use", user_id=user_id, task_id=task_id)
        root = self._root / f"u{int(user_id)}" / f"t{int(task_id)}" / f"s{next(_ids)}"
        root.mkdir(parents=True, exist_ok=True)
        s = LocalSession(root, self._python)
        self._sessions[s.id] = s
        return s

    async def stop(self, session_id: str) -> None:
        if (s := self._sessions.pop(session_id, None)) is not None:
            await s.close()

    async def health(self) -> BackendHealth:
        net = "isolated network" if _probe_unshare() else "network blocked in Python only"
        return BackendHealth(ok=True, detail=f"local subprocess, {net}")


class LocalWorkspaceStore(MetaReads):
    """Dev store: bytes under root/u{user_id}/{path}, metadata in the real table."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (get_settings().data_dir / "workspaces")

    def _host(self, user_id: int, path: str) -> Path:
        base = (self._root / f"u{int(user_id)}").resolve()
        target = (base / guard(path)).resolve()
        if not target.is_relative_to(base):
            raise SandboxPathError(f"path resolves outside the workspace: {path[:80]!r}")
        return target

    async def put(self, user_id, path, data, *, provenance, cls=None, task_id=None):
        async def write(rel, _cls):
            target = self._host(user_id, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(data))
        return await put_with_quota(user_id, path, data, provenance=provenance, cls=cls, task_id=task_id,
                                    write=write)

    async def get(self, user_id, path):
        rel = await self._require(user_id, path)
        return self._host(user_id, rel).read_bytes()

    async def delete(self, user_id, path):
        rel = guard(path)
        self._host(user_id, rel).unlink(missing_ok=True)
        await repo_machine.soft_delete_file(user_id, rel)

    async def purge_user(self, user_id):
        shutil.rmtree(self._root / f"u{int(user_id)}", ignore_errors=True)
        await repo_machine.purge_user(user_id)
