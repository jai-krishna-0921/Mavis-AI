# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""HTML mail body to text for Outlook, Teams and Gmail, parsed in an isolated worker process.

The parse and the hide policy live in `html_core` (see its docstring for the policy). A hostile mail must
never exhaust the box or freeze the bot, and the parser is a C library whose cost on pathological markup
cannot be bounded by reading the markup first (five textual estimators were beaten). So the work runs in a
small persistent pool of worker processes (started lazily, at most WORKERS of them):
- the address space of a worker is capped (RLIMIT_AS) and a request has a CPU backstop (RLIMIT_CPU);
- each request has a wall-clock timeout; a timeout, a dead worker, a memory error or any other failure kills
  and replaces the worker and answers with NOTICE, cut=True: fail closed, never a guess;
- input beyond MAX_HTML characters, or style text beyond its budget, answers NOTICE too (the markup is never
  split from the styles that apply to it).

The functions here block while the worker runs: code on the event loop calls them through
`asyncio.to_thread` (or the `a`-prefixed wrappers).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import os
import queue
import select
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import structlog

from mavis.config import get_settings
from mavis.tools.integrations.native import html_core
from mavis.tools.integrations.native.html_core import (  # noqa: F401 - re-exported for callers and tests
    _SKIP,
    MAX_HTML,
    NOTICE,
    declarations,
    hiding_rules,
    strip_comments,
    style_hides,
)

WORKERS = 2
TIMEOUT_S: float | None = None  # wall clock per request; None: the html_parse_timeout_s setting (2.0)
PARSE_BUDGET_S: float | None = None  # per tool call (see parse_budget); None: html_parse_budget_s (4.0)
START_TIMEOUT_S = 20.0  # a worker importing selectolax; not part of a request's timeout
QUEUE_TIMEOUT_S = 30.0  # waiting for a free worker behind other requests
RECYCLE_AFTER = 2000  # requests per worker before it is replaced (bounds any slow leak)
_SCRIPT = str(Path(html_core.__file__).resolve())
_ARGV = [sys.executable, "-I", _SCRIPT]
FAILED = (NOTICE, True)
log = structlog.get_logger(__name__)


def timeout_s() -> float:
    return TIMEOUT_S if TIMEOUT_S is not None else get_settings().html_parse_timeout_s


def budget_s() -> float:
    return PARSE_BUDGET_S if PARSE_BUDGET_S is not None else get_settings().html_parse_budget_s


class _Failed(Exception):
    """A request that could not be answered; `reason` is what is logged (never any content)."""

    def __init__(self, reason: str = "failed") -> None:
        super().__init__(reason)
        self.reason = reason


def _ready(fd: int, event: int, deadline: float) -> bool:
    """Wait until `fd` is readable or writable (poll: select() fails for descriptors of 1024 and up). A
    hang-up or error also counts: the next read or write then reports the death."""
    wait = deadline - time.monotonic()
    if wait <= 0:
        return False
    poller = select.poll()
    poller.register(fd, event)
    return bool(poller.poll(wait * 1000))


class _Worker:
    """One child running html_core as a script. Used by one thread at a time."""

    def __init__(self) -> None:
        self.calls = 0
        self.proc = subprocess.Popen(  # noqa: S603 - our own interpreter and file
            _ARGV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, cwd="/")
        self._in, self._out = self.proc.stdin.fileno(), self.proc.stdout.fileno()  # type: ignore[union-attr]
        os.set_blocking(self._in, False)
        os.set_blocking(self._out, False)
        try:
            if self._read(1, time.monotonic() + START_TIMEOUT_S) != b"R":
                raise _Failed("start_failed")
        except BaseException:
            self.kill()
            raise

    def _read(self, n: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        while n > 0:
            if not _ready(self._out, select.POLLIN, deadline):
                raise _Failed("timeout")
            try:
                chunk = os.read(self._out, min(n, 1 << 20))
            except BlockingIOError:
                continue
            if not chunk:
                raise _Failed("worker_died")
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def _write(self, data: bytes, deadline: float) -> None:
        view = memoryview(data)
        while view:
            if not _ready(self._in, select.POLLOUT, deadline):
                raise _Failed("timeout")
            try:
                view = view[os.write(self._in, view[:1 << 16]):]
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise _Failed("worker_died") from exc

    def call(self, markup: str, limit: int, styles: bool) -> tuple[str, bool]:
        deadline = time.monotonic() + timeout_s()
        data = markup.encode("utf-8", "replace")
        self._write(struct.pack(">BII", int(styles), min(limit, 2**32 - 1), len(data)) + data, deadline)
        status, size = struct.unpack(">BI", self._read(5, deadline))
        text = self._read(size, deadline).decode("utf-8", "replace")
        self.calls += 1
        if status == 3:  # a memory error or other fault: the process state is not trusted
            raise _Failed("worker_error")
        return text, status != 0

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()  # type: ignore[union-attr]
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _failed(reason: str, markup: str) -> tuple[str, bool]:
    """The fail-closed answer; the reason and the size are logged, never the content."""
    log.warning("html_parse_failed_closed", reason=reason, chars=len(markup))
    return FAILED


class _Pool:
    """At most WORKERS workers, created on demand. A slot is `None` until a worker is started for it."""

    def __init__(self) -> None:
        self._slots: queue.Queue[_Worker | None] = queue.Queue()
        for _ in range(WORKERS):
            self._slots.put(None)
        self._live: set[_Worker] = set()
        self._lock = threading.Lock()

    def run(self, markup: str, limit: int, styles: bool) -> tuple[str, bool]:
        try:
            worker = self._slots.get(timeout=QUEUE_TIMEOUT_S)
        except queue.Empty:
            return _failed("queue_timeout", markup)
        keep: _Worker | None = None
        try:
            if worker is not None and worker.proc.poll() is not None:  # died while idle
                with self._lock:
                    self._live.discard(worker)
                worker.kill()
                worker = None
            if worker is None:
                try:
                    worker = _Worker()
                except (_Failed, OSError):
                    return _failed("start_failed", markup)
                with self._lock:
                    self._live.add(worker)
            result = worker.call(markup, limit, styles)
            keep = worker
            if result == FAILED:
                _failed("too_complex", markup)
            return result
        except _Failed as exc:
            return _failed(exc.reason, markup)
        except (OSError, ValueError, struct.error):
            return _failed("protocol_error", markup)
        finally:
            if worker is not None and (keep is None or keep.calls >= RECYCLE_AFTER):
                with self._lock:
                    self._live.discard(worker)
                worker.kill()
                keep = None
            self._slots.put(keep)

    def close(self) -> None:
        with self._lock:
            workers, self._live = list(self._live), set()
        for worker in workers:
            worker.kill()


_pool = _Pool()
atexit.register(_pool.close)


class _Budget:
    """The parse time one tool call (one list, one thread) may spend in all."""

    def __init__(self) -> None:
        self.left = budget_s()


_budget: contextvars.ContextVar[_Budget | None] = contextvars.ContextVar("html_parse_budget", default=None)


@contextlib.contextmanager
def parse_budget() -> Iterator[_Budget]:
    """Within this scope the parses of one call share a time budget for the ones that fail closed (timeout,
    death, limit): once it is spent every further body gets the notice without a parse, so a list of hostile
    mails costs the budget plus one timeout. Legitimate parses are not charged."""
    budget = _Budget()
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)


def html_to_text_limited(markup: str, limit: int, *, styles: bool = True) -> tuple[str, bool]:
    """(text, cut): `cut` is True when the text hit `limit`, a browser's tree is known to differ from ours
    past some point, or the message could not be read safely (then the text is NOTICE). `styles=False` hides
    by tag only (no style, class or attribute rules): the Gmail reader. Blocks while the worker runs."""
    if not markup.strip():
        return "", False
    if len(markup) > MAX_HTML:
        return _failed("too_large", markup)
    budget = _budget.get()
    if budget is None:
        return _pool.run(markup, limit, styles)
    if budget.left <= 0:
        return _failed("budget_spent", markup)
    started = time.monotonic()
    result = _pool.run(markup, limit, styles)
    if result == FAILED:  # only what failed closed is charged: a legitimate parse costs the call nothing
        budget.left -= time.monotonic() - started
    return result


def html_to_text(markup: str, limit: int = MAX_HTML) -> str:
    return html_to_text_limited(markup, limit)[0]


# Parses wait on a worker process, so async code runs them on this small executor of their own: a hostile
# mail can hold a thread for the timeout, and the default executor is shared with embeddings, S3 and webhooks.
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="html-parse")
atexit.register(_executor.shutdown, wait=False, cancel_futures=True)


async def offload(fn: Callable[..., Any], *args: Any) -> Any:
    """Run `fn(*args)` (which may parse any number of bodies) off the event loop, under one parse budget."""
    def run() -> Any:
        with parse_budget():
            return fn(*args)

    return await asyncio.get_running_loop().run_in_executor(_executor, run)


async def ahtml_to_text_limited(markup: str, limit: int, *, styles: bool = True) -> tuple[str, bool]:
    return await offload(lambda: html_to_text_limited(markup, limit, styles=styles))


async def ahtml_to_text(markup: str, limit: int = MAX_HTML) -> str:
    return await offload(html_to_text, markup, limit)
