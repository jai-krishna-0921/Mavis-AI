"""MachineRuntime: the only thing tools talk to (spec 5, 6.2, 9.3).

Per task, lazily: quota check, per-user concurrency, a global slot, a machine_sessions row, then
Sandbox.open with a timeout of the remaining task budget plus grace. On open the user's workspace is
written in (whole when small, else recent inbox files and .mavis/). After every exec the session is
listed and diffed; changed files go to the WorkspaceStore with code-set provenance; new out/ files
become artefacts and are delivered at once (user tasks). `release` always runs in _drive's finally."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import shlex
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import structlog

from mavis.config import get_settings
from mavis.domain.errors import ActionFailed
from mavis.domain.tasks import TaskOrigin, TaskStatus
from mavis.machine.errors import MachineBusy, QuotaExceeded, SessionUserMismatch
from mavis.machine.paths import artifact_file, guard, is_hidden, safe_name
from mavis.machine.ports import (
    ExecRequest,
    ExecResult,
    FileEntry,
    Provenance,
    Sandbox,
    SandboxSession,
    WorkspaceStore,
)
from mavis.machine.quota import BUSY_TEXT, CONCURRENT_TEXT, GlobalSlots, Meter
from mavis.store.db import utcnow
from mavis.store.repo import machine as repo
from mavis.store.repo import tasks

log = structlog.get_logger(__name__)
DeliverFn = Callable[..., Awaitable[bool]]
SYNC_OUT_BOUND_S = 20.0


def missing_probe(imports: dict[str, str]) -> str:
    """Python that prints the import names whose package is not installed. A module that imports is not
    enough: an old distribution can own the same import name (`fpdf` from pyfpdf is not fpdf2)."""
    return (
        "import importlib.metadata as md\n"
        "import importlib.util as u\n"
        "def ok(mod, dist):\n"
        "    try:\n"
        "        md.distribution(dist)\n"
        "    except md.PackageNotFoundError:\n"
        "        return False\n"
        "    return u.find_spec(mod) is not None\n"
        f"print(' '.join(m for m, d in {dict(imports)!r}.items() if not ok(m, d)))\n"
    )


class _Open:
    __slots__ = ("kind", "listing", "opened", "session", "user_id")

    def __init__(self, session: SandboxSession, user_id: int, kind: str, opened: float) -> None:
        self.session, self.user_id, self.kind, self.opened = session, user_id, kind, opened
        self.listing: dict[str, str | None] = {}


class MachineRuntime:
    def __init__(self, sandbox: Sandbox, store: WorkspaceStore, *, browser=None, meter: Meter | None = None,
                 slots: GlobalSlots | None = None, deliver: DeliverFn | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.sandbox, self.store, self.browser_backend = sandbox, store, browser
        self.meter, self.slots = meter or Meter(), slots or GlobalSlots()
        self._deliver, self._clock = deliver, clock
        self._sessions: dict[int, _Open] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._untrusted: set[int] = set()
        self._log: dict[int, list[ExecResult]] = {}

    # --- trust --------------------------------------------------------------------
    def mark_untrusted(self, task_id: int) -> None:
        self._untrusted.add(task_id)

    def untrusted(self, task_id: int) -> bool:
        return task_id in self._untrusted

    def exec_log(self, task_id: int) -> list[ExecResult]:
        return list(self._log.get(task_id, []))

    # --- sessions -----------------------------------------------------------------
    async def session(self, user_id: int, task_id: int) -> SandboxSession:
        async with self._locks.setdefault(task_id, asyncio.Lock()):
            live = self._sessions.get(task_id)
            if live is not None:
                if live.user_id != user_id:
                    raise SessionUserMismatch(f"task {task_id} session belongs to another user")
                return live.session
            for row in await repo.sessions_for_task(task_id):
                if row.user_id != user_id:
                    raise SessionUserMismatch(f"task {task_id} session belongs to another user")
            if (refusal := await self.meter.refusal(user_id)) is not None:
                raise QuotaExceeded(refusal)
            s = get_settings()
            others = await repo.open_count_for_user(user_id, exclude_task=task_id)
            if others >= s.machine_max_concurrent_per_user:
                raise QuotaExceeded(CONCURRENT_TEXT)
            if not await self.slots.acquire(task_id):
                raise MachineBusy(BUSY_TEXT)
            timeout = int(await self._remaining_s(task_id) + s.machine_session_grace_s)
            try:
                session = await self.sandbox.open(user_id=user_id, task_id=task_id, timeout_s=timeout)
            except Exception:
                await self.slots.release(task_id)
                raise
            await repo.open_session(user_id=user_id, task_id=task_id, kind="code", backend=self.sandbox.name,
                                    session_id=session.id, deadline_at=utcnow() + timedelta(seconds=timeout))
            live = _Open(session, user_id, "code", self._clock())
            self._sessions[task_id] = live
            await self._sync_in(user_id, task_id, live)
            live.listing = {e.path: e.sha256 for e in await session.list()}
            return session

    async def _remaining_s(self, task_id: int) -> float:
        from mavis.agents.task_clock import current_clock

        clock = current_clock.get()
        total = clock.total_s if clock is not None else get_settings().machine_task_timeout_s
        task = await tasks.get(task_id)
        started = task.started_at if task is not None and task.started_at else utcnow()
        return max(60.0, total - (utcnow() - started).total_seconds())

    async def _sync_in(self, user_id: int, task_id: int, live: _Open) -> None:
        s = get_settings()
        files = await self.store.list(user_id)
        total = sum(f.size for f in files)
        if total > s.workspace_sync_max_mb * 1024 * 1024:
            cutoff = utcnow() - timedelta(hours=24)
            rows = {r.path: r for r in await repo.list_files(user_id)}
            files = [f for f in files if f.path.startswith(".mavis/")
                     or (f.path.startswith("inbox/") and rows[f.path].updated_at >= cutoff)]
        for f in files:
            if f.provenance.untrusted:
                self.mark_untrusted(task_id)
            await live.session.write(f.path, await self.store.get(user_id, f.path))

    # --- operations -----------------------------------------------------------------
    async def exec(
        self, user_id: int, task_id: int, req: ExecRequest, *, record: bool = True
    ) -> ExecResult:
        session = await self.session(user_id, task_id)
        req = req.model_copy(update={"timeout_s": max(1, min(int(req.timeout_s),
                                                             get_settings().sandbox_exec_max_s))})
        result = await session.exec(req)
        saved, skipped = await self._sync_out_checked(user_id, task_id)
        result.changed = saved
        if skipped:
            result.stderr = (result.stderr + "\n" if result.stderr else "") + (
                "Not saved to the workspace because storage is full: " + ", ".join(skipped))
        if record:  # internal calls (installs, builders) are not attempts the user should see
            self._log.setdefault(task_id, []).append(result)
        return result

    async def install(self, user_id: int, task_id: int, packages: list[str]) -> ExecResult:
        """Wheels are resolved on the worker (no internet in the sandbox) and installed from the workspace."""
        from mavis.machine.wheels import WheelCache

        wheels = await WheelCache().resolve(packages)
        for filename, blob in wheels:
            await self.write_in(
                user_id, task_id, f".mavis/wheels/{filename}", blob, provenance=Provenance.MAVIS
            )
        names = " ".join(shlex.quote(p) for p in packages)
        cmd = f"python -m pip install --no-index --find-links .mavis/wheels --quiet {names}"
        req = ExecRequest(language="shell", timeout_s=180, code=cmd)
        return await self.exec(user_id, task_id, req, record=False)

    async def ensure_packages(self, user_id: int, task_id: int, imports: dict[str, str]) -> None:
        """Install the packages whose import names are missing in the session (import name -> package)."""
        req = ExecRequest(language="python", code=missing_probe(imports), timeout_s=30)
        res = await self.exec(user_id, task_id, req, record=False)
        missing = sorted({imports[m] for m in res.stdout.split() if m in imports})
        if missing:
            res = await self.install(user_id, task_id, missing)
            if not res.ok:
                why = (res.stderr or res.error or "")[-300:]
                raise ActionFailed(f"Could not install {', '.join(missing)}: {why}")

    async def write_in(self, user_id: int, task_id: int, path: str, data: bytes, *,
                       provenance: Provenance) -> None:
        session = await self.session(user_id, task_id)
        if provenance.untrusted:
            self.mark_untrusted(task_id)
        await self.store.put(user_id, path, data, provenance=provenance, task_id=task_id)
        await session.write(path, data)
        # our own write: record its hash so it is not mistaken for a new file made by code
        self._sessions[task_id].listing[path] = hashlib.sha256(data).hexdigest()

    async def attach(self, user_id: int, task_id: int, path: str) -> None:
        meta = await self.store.meta(user_id, path)
        if meta is None:
            raise FileNotFoundError(path)
        await self.write_in(user_id, task_id, path, await self.store.get(user_id, path),
                            provenance=meta.provenance)

    async def read_out(self, user_id: int, task_id: int, path: str) -> bytes:
        return await (await self.session(user_id, task_id)).read(path)

    async def _sync_out_checked(self, user_id: int, task_id: int, *, final: bool = False
                                ) -> tuple[list[FileEntry], list[str]]:
        live = self._sessions.get(task_id)
        if live is None:
            return [], []
        s = get_settings()
        now = {e.path: e for e in await live.session.list()}
        changed = [e for p, e in now.items()
                   if live.listing.get(p, "missing") != e.sha256 and not is_hidden(p)]
        prov = Provenance.GENERATED_TAINTED if self.untrusted(task_id) else Provenance.GENERATED_CLEAN
        saved: list[FileEntry] = []
        skipped: list[str] = []
        for e in changed:
            if e.size > s.machine_file_max_mb * 1024 * 1024:
                log.info("machine.file_too_big", task_id=task_id, size=e.size)
                now.pop(e.path)  # not tracked, so a smaller rewrite later is still seen
                continue
            data = await live.session.read(e.path)
            try:
                await self.store.put(user_id, e.path, data, provenance=prov, task_id=task_id)
            except QuotaExceeded:
                skipped.append(e.path)
                now.pop(e.path)  # retried at the next sync, once the user frees space
                continue
            saved.append(e)
            if e.path.startswith("out/") and live.listing.get(e.path, "missing") == "missing":
                await self._new_artifact(user_id, task_id, e.path, data, final=final)
        live.listing = {p: e.sha256 for p, e in now.items()}
        return saved, skipped

    async def add_artifact(
        self, user_id: int, task_id: int, name: str, data: bytes, *, title: str = ""
    ) -> int:
        local = artifact_file(user_id, task_id, name)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return await tasks.add_artifact(
            task_id, user_id, kind=local.suffix.lstrip(".") or "file", path=str(local),
            mime=mimetypes.guess_type(local.name)[0] or "application/octet-stream",
            title=title or local.name, size=len(data),
        )

    async def deliver(self, user_id: int, task_id: int, aid: int, **kw) -> bool:
        deliver = self._deliver
        if deliver is None:
            from mavis.initiative.task_delivery import deliver_artifact_now as deliver
        return await deliver(user_id, task_id, aid, **kw)

    async def _new_artifact(self, user_id: int, task_id: int, path: str, data: bytes, *, final: bool) -> None:
        name = safe_name(Path(path).name)
        aid = await self.add_artifact(user_id, task_id, name, data)
        task = await tasks.get(task_id)
        if task is not None and task.origin == TaskOrigin.USER.value:
            # the closing sync still hands over what was made, unless the user cancelled the task
            end = final and task.status != TaskStatus.CANCELLED.value
            if end or not final:
                await self.deliver(user_id, task_id, aid, **({"end_of_task": True} if end else {}))
        from mavis.channels.progress_card import get_cards

        await get_cards().tool_called(task_id, f"made {name}")

    # --- document builders and what ran -----------------------------------------------
    async def _run_script(self, user_id: int, task_id: int, script: str, *args: str,
                          timeout_s: int) -> ExecResult:
        """Run a builder in the session. The arguments are code-made paths, quoted as Python literals."""
        code = (
            "import runpy, sys\n"
            f"sys.argv = {[script, *args]!r}\n"
            f"runpy.run_path({script!r}, run_name='__main__')\n"
        )
        req = ExecRequest(language="python", code=code, timeout_s=timeout_s)
        return await self.exec(user_id, task_id, req, record=False)

    async def build(
        self, user_id: int, task_id: int, builder: str, data: dict, out_name: str
    ) -> ExecResult:
        """Run a builder script in the session on `data` (a JSON file, never formatted into code)."""
        import json
        import uuid

        from mavis.machine.builders import BUILDER_IMPORTS, load

        await self.ensure_packages(user_id, task_id, BUILDER_IMPORTS[builder])
        script, data_path = f".mavis/builders/{builder}.py", f".mavis/data/{uuid.uuid4().hex}.json"
        await self.write_in(user_id, task_id, script, load(builder).encode(), provenance=Provenance.MAVIS)
        prov = Provenance.GENERATED_TAINTED if self.untrusted(task_id) else Provenance.GENERATED_CLEAN
        await self.write_in(
            user_id, task_id, data_path, json.dumps(data, ensure_ascii=False).encode(), provenance=prov
        )
        target = f"out/{safe_name(out_name)}"
        return await self._run_script(user_id, task_id, script, data_path, target, timeout_s=180)

    async def extract_text(self, user_id: int, task_id: int, path: str, max_chars: int) -> str:
        """Text of a workspace file (PDF, DOCX, XLSX, CSV or text), extracted inside the session."""
        import json

        from mavis.machine.builders import BUILDER_IMPORTS, load

        path = guard(path)
        await self.attach(user_id, task_id, path)
        await self.ensure_packages(user_id, task_id, BUILDER_IMPORTS["extract"])
        script, data = ".mavis/builders/extract.py", ".mavis/data/extract.json"
        await self.write_in(user_id, task_id, script, load("extract").encode(), provenance=Provenance.MAVIS)
        await self.write_in(
            user_id, task_id, data, json.dumps({"max_chars": int(max_chars)}).encode(),
            provenance=Provenance.MAVIS,
        )
        res = await self._run_script(user_id, task_id, script, data, path, timeout_s=60)
        return res.stdout if res.ok else f"Could not read {path}: {res.stderr[-300:] or res.error}"

    def what_i_ran(self, task_id: int) -> str:
        """The "What I ran" block: exit code per attempt and the last 15 lines of the last output."""
        runs = self._log.get(task_id, [])
        if not runs:
            return ""
        lines = ["What I ran:"]
        for i, r in enumerate(runs, start=1):
            lines.append(f"- attempt {i}: " + ("timed out" if r.timed_out else f"exit {r.exit_code}"))
        tail = "\n".join(runs[-1].stdout.strip().splitlines()[-15:])
        if tail:
            lines += ["Last output:", "```", tail, "```"]
        return "\n".join(lines)

    # --- lifecycle --------------------------------------------------------------------
    async def release(self, task_id: int) -> None:
        live = self._sessions.get(task_id)
        if live is not None:
            try:
                async with asyncio.timeout(SYNC_OUT_BOUND_S):
                    # files made in the last moments still count
                    await self._sync_out_checked(live.user_id, task_id, final=True)
            except Exception as exc:  # noqa: BLE001 - best effort
                log.warning("machine.final_sync_failed", task_id=task_id, error=type(exc).__name__)
            finally:
                self._sessions.pop(task_id, None)
            await self._close(live, task_id, "closed")
        await self.slots.release(task_id)
        self._untrusted.discard(task_id)
        self._locks.pop(task_id, None)
        self._log.pop(task_id, None)

    async def _close(self, live: _Open, task_id: int, status: str) -> None:
        wall = self._clock() - live.opened
        try:
            await live.session.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("machine.close_failed", error=type(exc).__name__)
        cost = await self.meter.record(live.user_id, live.kind, task_id, live.session.id, wall,
                                       provider=self.sandbox.name)
        await repo.close_session(live.session.id, status, wall, cost)

    async def _stop_row(self, row, status: str = "stopped") -> None:
        """Stop a session by id and book the time it ran (the row's own clock when it is not live here)."""
        try:
            await self.sandbox.stop(row.session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("machine.stop_failed", error=type(exc).__name__)
        live = self._sessions.get(row.task_id)
        if live is not None and live.session.id == row.session_id:
            wall = self._clock() - live.opened
        else:
            end = min(utcnow(), row.deadline_at) if row.deadline_at is not None else utcnow()
            wall = max(0.0, (end - row.opened_at).total_seconds())
        try:
            cost = await self.meter.record(row.user_id, row.kind, row.task_id, row.session_id, wall,
                                           provider=row.backend)
        except Exception as exc:  # noqa: BLE001 - never leave the row open because metering failed
            log.warning("machine.meter_failed", error=type(exc).__name__)
            cost = 0.0
        await repo.close_session(row.session_id, status, wall, cost)

    async def cancel(self, task_id: int) -> None:
        for row in await repo.sessions_for_task(task_id):
            await self._stop_row(row)
        self._sessions.pop(task_id, None)
        await self.slots.release(task_id)

    async def reap(self) -> int:
        n = 0
        for row in await repo.open_sessions():
            task = await tasks.get(row.task_id)
            terminal = task is None or task.status not in (TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL)
            past = row.deadline_at is not None and row.deadline_at < utcnow()
            if (terminal or past) and row.task_id not in self._sessions:
                await self._stop_row(row)
                await self.slots.release(row.task_id)
                n += 1
        return n

    async def stop_all(self) -> int:
        n = 0
        for row in await repo.open_sessions():
            await self.cancel(row.task_id)
            n += 1
        return n

    async def run_reaper_forever(self, interval_s: float | None = None) -> None:
        from mavis.worker.locks import claim

        every = interval_s or get_settings().machine_reaper_interval_s
        while True:
            try:
                if await claim("machine:reaper", every * 0.9) and (n := await self.reap()):
                    log.info("machine.reaped", count=n)
            except Exception:  # noqa: BLE001 - the reaper must survive transient errors
                log.exception("machine.reaper_failed")
            await asyncio.sleep(every)
