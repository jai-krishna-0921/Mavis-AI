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
import os
import queue
import select
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

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
TIMEOUT_S = 2.0  # wall clock for one request (a 1 MB newsletter parses in about a tenth of it)
START_TIMEOUT_S = 20.0  # a worker importing selectolax; not part of a request's timeout
QUEUE_TIMEOUT_S = 30.0  # waiting for a free worker behind other requests
RECYCLE_AFTER = 2000  # requests per worker before it is replaced (bounds any slow leak)
_SCRIPT = str(Path(html_core.__file__).resolve())
FAILED = (NOTICE, True)


class _Failed(Exception):
    pass


class _Worker:
    """One child running html_core as a script. Used by one thread at a time."""

    def __init__(self) -> None:
        self.calls = 0
        self.proc = subprocess.Popen(  # noqa: S603 - our own interpreter and file
            [sys.executable, "-I", _SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, cwd="/")
        self._in, self._out = self.proc.stdin.fileno(), self.proc.stdout.fileno()  # type: ignore[union-attr]
        os.set_blocking(self._in, False)
        os.set_blocking(self._out, False)
        try:
            if self._read(1, time.monotonic() + START_TIMEOUT_S) != b"R":
                raise _Failed
        except BaseException:
            self.kill()
            raise

    def _read(self, n: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        while n > 0:
            wait = deadline - time.monotonic()
            if wait <= 0 or not select.select([self._out], [], [], wait)[0]:
                raise _Failed
            try:
                chunk = os.read(self._out, min(n, 1 << 20))
            except BlockingIOError:
                continue
            if not chunk:
                raise _Failed  # the worker died
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def _write(self, data: bytes, deadline: float) -> None:
        view = memoryview(data)
        while view:
            wait = deadline - time.monotonic()
            if wait <= 0 or not select.select([], [self._in], [], wait)[1]:
                raise _Failed
            try:
                view = view[os.write(self._in, view[:1 << 16]):]
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise _Failed from exc

    def call(self, markup: str, limit: int, styles: bool) -> tuple[str, bool]:
        deadline = time.monotonic() + TIMEOUT_S
        data = markup.encode("utf-8", "replace")
        self._write(struct.pack(">BII", int(styles), min(limit, 2**32 - 1), len(data)) + data, deadline)
        status, size = struct.unpack(">BI", self._read(5, deadline))
        text = self._read(size, deadline).decode("utf-8", "replace")
        self.calls += 1
        if status == 3:  # a memory error or other fault: the process state is not trusted
            raise _Failed
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
            return FAILED
        keep: _Worker | None = None
        try:
            if worker is not None and worker.proc.poll() is not None:  # died while idle
                with self._lock:
                    self._live.discard(worker)
                worker.kill()
                worker = None
            if worker is None:
                worker = _Worker()
                with self._lock:
                    self._live.add(worker)
            result = worker.call(markup, limit, styles)
            keep = worker
            return result
        except (_Failed, OSError, ValueError, struct.error):
            return FAILED
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


def html_to_text_limited(markup: str, limit: int, *, styles: bool = True) -> tuple[str, bool]:
    """(text, cut): `cut` is True when the text hit `limit`, a browser's tree is known to differ from ours
    past some point, or the message could not be read safely (then the text is NOTICE). `styles=False` hides
    by tag only (no style, class or attribute rules): the Gmail reader. Blocks while the worker runs."""
    if not markup.strip():
        return "", False
    if len(markup) > MAX_HTML:
        return FAILED
    return _pool.run(markup, limit, styles)


def html_to_text(markup: str, limit: int = MAX_HTML) -> str:
    return html_to_text_limited(markup, limit)[0]


async def ahtml_to_text_limited(markup: str, limit: int, *, styles: bool = True) -> tuple[str, bool]:
    return await asyncio.to_thread(html_to_text_limited, markup, limit, styles=styles)


async def ahtml_to_text(markup: str, limit: int = MAX_HTML) -> str:
    return await asyncio.to_thread(html_to_text, markup, limit)
