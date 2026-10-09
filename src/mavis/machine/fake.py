"""Test doubles: an in-memory sandbox (files only, scripted exec results) and workspace store."""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable

from mavis.machine.paths import guard
from mavis.machine.ports import BackendHealth, ExecRequest, ExecResult, FileEntry
from mavis.machine.s3store import MetaReads, put_with_quota
from mavis.store.repo import machine as repo_machine

_ids = itertools.count(1)


class FakeSession:
    def __init__(self, owner: FakeSandbox, user_id: int, task_id: int) -> None:
        self.id = f"fake-{next(_ids)}"
        self.user_id, self.task_id, self._owner = user_id, task_id, owner
        self.files: dict[str, bytes] = {}
        self.closed = False
        self.dead = False
        self.execs: list[ExecRequest] = []

    def _live(self) -> None:
        if self.closed or self.id in self._owner.stopped:
            raise ConnectionError("session stopped")

    async def exec(self, req: ExecRequest, on_output=None) -> ExecResult:
        self._live()
        self.execs.append(req)
        if self._owner.on_exec is not None:
            res = self._owner.on_exec(req, self.files)
        elif self._owner.results:
            res = self._owner.results.pop(0)
        else:
            res = ExecResult(ok=True, exit_code=0)
        if res.timed_out and self._owner.die_on_timeout:  # like AgentCore: the session is stopped
            self.dead = self.closed = True
            self._owner.stopped.append(self.id)
        return res

    async def write(self, path: str, data: bytes) -> None:
        self._live()
        self.files[guard(path)] = bytes(data)

    async def read(self, path: str) -> bytes:
        self._live()
        p = guard(path)
        if p not in self.files:
            raise FileNotFoundError(p)
        return self.files[p]

    async def list(self, path: str = "") -> list[FileEntry]:
        self._live()
        prefix = guard(path) + "/" if path else ""
        return [FileEntry(path=p, size=len(b), sha256=hashlib.sha256(b).hexdigest())
                for p, b in sorted(self.files.items()) if p.startswith(prefix)]

    async def remove(self, path: str) -> None:
        self._live()
        self.files.pop(guard(path), None)

    async def close(self) -> None:
        self.closed = True


class FakeSandbox:
    name = "fake"

    def __init__(self, results: list[ExecResult] | None = None) -> None:
        self.results = list(results or [])
        self.sessions: dict[str, FakeSession] = {}
        self.opened: list[tuple[int, int]] = []
        self.stopped: list[str] = []
        self.fail_open: Exception | None = None
        self.die_on_timeout = True
        self.on_exec: Callable[[ExecRequest, dict[str, bytes]], ExecResult] | None = None

    async def open(self, *, user_id: int, task_id: int, timeout_s: int) -> FakeSession:
        if self.fail_open is not None:
            raise self.fail_open
        s = FakeSession(self, user_id, task_id)
        self.sessions[s.id] = s
        self.opened.append((user_id, task_id))
        return s

    async def stop(self, session_id: str) -> None:
        if session_id not in self.stopped:
            self.stopped.append(session_id)
        if (s := self.sessions.get(session_id)) is not None:
            s.closed = True

    async def health(self) -> BackendHealth:
        return BackendHealth(ok=self.fail_open is None)


class MemoryWorkspaceStore(MetaReads):
    """Bytes in a dict, metadata in the real workspace_files table (so quota logic is the same code)."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[int, str], bytes] = {}

    async def put(self, user_id, path, data, *, provenance, cls=None, task_id=None):
        async def write(rel, _cls):
            self.blobs[(int(user_id), rel)] = bytes(data)
        return await put_with_quota(user_id, path, data, provenance=provenance, cls=cls, task_id=task_id,
                                    write=write)

    async def get(self, user_id, path):
        rel = await self._require(user_id, path)
        try:
            return self.blobs[(int(user_id), rel)]
        except KeyError:
            raise FileNotFoundError(rel) from None

    async def delete(self, user_id, path):
        rel = guard(path)
        self.blobs.pop((int(user_id), rel), None)
        await repo_machine.soft_delete_file(user_id, rel)

    async def purge_user(self, user_id):
        for key in [k for k in self.blobs if k[0] == int(user_id)]:
            self.blobs.pop(key)
        await repo_machine.purge_user(user_id)
