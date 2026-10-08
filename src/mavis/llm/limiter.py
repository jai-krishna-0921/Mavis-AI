"""LLM slot limiter backends (spec 8). SharedLimiter: Redis + Lua, safe across processes. LocalLimiterAdapter:
today's in-process _Limiter. FallingBackLimiter: Redis, and the local limiter while Redis is unreachable."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
import weakref
from collections.abc import Callable
from typing import Any, NamedTuple, Protocol

import structlog

from mavis import bus
from mavis.config import get_settings
from mavis.domain.errors import LLMError
from mavis.llm import limiter_lua, share
from mavis.llm.context import llm_user_id

log = structlog.get_logger(__name__)
_RANK = {"interactive": 0, "background": 1, "best_effort": 2}
_tasks: set[asyncio.Task] = set()
WAITER_TTL_MS = 5000  # a waiter that stops polling (dead process) drops out of the queue after this
HOLD_MARGIN_S = 15.0
QUEUE_VIEW_FRESH_S = 5.0
SECONDS_PER_QUEUED_CALL = 5.0


class Lease(NamedTuple):
    id: str
    provider: str
    priority: str = "interactive"  # the release needs it: best_effort holders are counted separately
    timeout_s: float | None = None  # a best_effort call's own timeout (mavis.llm.share), else no clamp


class LimiterBackend(Protocol):
    async def acquire(self, priority: str, wait_s: float) -> Lease | None: ...
    def release(self, lease: Lease | None) -> None: ...
    def touch(self) -> None: ...
    def estimate_wait_s(self) -> float: ...


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 - a holder's TTL expires it anyway
        log.debug("llm.limiter_background_failed", error=type(exc).__name__)


def spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(_quiet(coro))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# order of the policy constants in the Lua ARGV (after WAITER_TTL_MS)
_CONSTANT_ORDER = ("grace_s", "background_aging_s", "best_effort_aging_s", "min_gap_s", "lull_quiet_s",
                   "escape_after_s", "escape_chat_gap_s", "timeout_s", "escape_timeout_s")


def policy_constants() -> dict[str, float]:
    """The slot policy's numbers as they are now (mavis.llm.share and the limiter's own ageing; tests move
    the module globals)."""
    from mavis.llm import models

    return {"grace_s": models.INTERACTIVE_GRACE_S, "background_aging_s": models.BACKGROUND_AGING_S,
            "best_effort_aging_s": models.BEST_EFFORT_AGING_S, "min_gap_s": share.MIN_START_GAP_S,
            "lull_quiet_s": share.LULL_QUIET_S, "escape_after_s": share.ESCAPE_AFTER_S,
            "escape_chat_gap_s": share.ESCAPE_CHAT_GAP_S, "timeout_s": share.TIMEOUT_S,
            "escape_timeout_s": share.ESCAPE_TIMEOUT_S}


class SharedLimiter:
    def __init__(self, client, provider: str, slots: int, bg_max: int, user_max: int, hold_ttl_s: float,
                 constants: Callable[[], dict[str, float]] | None = None) -> None:
        self._r, self._prov = client, provider
        self._p = f"mavis:llm:{provider}:"
        self._slots, self._bg, self._um = slots, bg_max, user_max
        self._constants = constants or policy_constants
        self._ttl_ms = int(hold_ttl_s * 1000)
        self._try = client.register_script(limiter_lua.TRY_ACQUIRE)
        self._rel = client.register_script(limiter_lua.RELEASE)
        self._cancel = client.register_script(limiter_lua.CANCEL)
        self._queue_seen = (0, float("-inf"))  # (length, monotonic time) from the latest acquire attempt

    def _keys(self) -> list[str]:
        return [self._p + "holders", self._p + "queue", self._p + "last_used", self._p + "last_chat_start",
                self._p + "last_be_start"]

    async def acquire(self, priority: str, wait_s: float) -> Lease | None:
        rank = _RANK[priority]
        me = uuid.uuid4().hex
        uid = str(llm_user_id.get() or 0)
        since = int(time.time() * 1000)
        deadline = time.monotonic() + max(wait_s, 0.0)
        try:
            while True:
                c = self._constants()
                code, qlen, clamp_ms = await self._try(keys=self._keys(), args=[
                    int(time.time() * 1000), me, uid, rank, since, self._slots, self._bg, self._um,
                    self._ttl_ms, self._p, WAITER_TTL_MS, *[int(c[k] * 1000) for k in _CONSTANT_ORDER]])
                self._queue_seen = (int(qlen), time.monotonic())
                if int(code) == 1:
                    return Lease(me, self._prov, priority, int(clamp_ms) / 1000 if int(clamp_ms) else None)
                if int(code) == 2:  # one slot and other work is active: best_effort yields, the job retries
                    raise LLMError("LLM slot reserved for interactive work")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LLMError("timed out waiting for an LLM slot")
                pause = min(left, 0.25 + random.random() * 0.1)
                try:  # a release wakes the oldest waiter early; everyone also polls
                    await self._r.blpop([self._p + "wake:" + me], timeout=max(pause, 0.05))
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(pause)
        except BaseException:
            spawn(self._cancel(keys=[self._p + "queue"], args=[me, self._p]))
            raise

    def release(self, lease: Lease | None) -> None:
        if lease is not None:
            spawn(self._rel(keys=self._keys()[:2], args=[lease.id, self._p]))

    def touch(self) -> None:
        """A chat or task call just ended: best_effort keeps off the slot for the quiet period."""
        spawn(self._r.set(self._p + "last_used", int(time.time() * 1000), px=600_000))

    def estimate_wait_s(self) -> float:
        length, at = self._queue_seen
        if time.monotonic() - at > QUEUE_VIEW_FRESH_S:
            return 0.0
        return length * SECONDS_PER_QUEUED_CALL

    async def queue_len(self) -> int:
        return int(await self._r.zcard(self._p + "queue"))


class LocalLimiterAdapter:
    """The in-process `_Limiter` behind the backend interface. It looks the limiter up on every call, so a
    reset of models._limiters (tests, a new event loop) is seen at once."""

    def __init__(self, secondary: bool = False) -> None:
        self._secondary = secondary

    @property
    def _inner(self):
        from mavis.llm import models

        return models._limiter(self._secondary)

    async def acquire(self, priority: str, wait_s: float) -> Lease | None:
        clamp = await self._inner.acquire(priority, wait_s)
        return Lease("local", "local", priority, clamp)

    def release(self, lease: Lease | None) -> None:
        self._inner.release(best_effort=lease is not None and lease.priority == "best_effort")

    def touch(self) -> None:
        self._inner.touch()

    def estimate_wait_s(self) -> float:
        return float(len(self._inner._waiters)) * SECONDS_PER_QUEUED_CALL


class FallingBackLimiter:
    """Redis when it answers; the local limiter when it does not (never block chat on Redis trouble)."""

    def __init__(self, shared: SharedLimiter | None, local: LocalLimiterAdapter) -> None:
        self._shared, self._local = shared, local
        self._warned = float("-inf")

    async def acquire(self, priority: str, wait_s: float) -> Lease | None:
        if self._shared is not None:
            try:
                return await self._shared.acquire(priority, wait_s)
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001
                if time.monotonic() - self._warned > 60:
                    self._warned = time.monotonic()
                    log.warning("llm.limiter_local_fallback", error=type(exc).__name__)
        return await self._local.acquire(priority, wait_s)

    def release(self, lease: Lease | None) -> None:
        if lease is not None and lease.provider == "local":
            self._local.release(lease)
        elif self._shared is not None:
            self._shared.release(lease)

    def touch(self) -> None:
        self._local.touch()
        if self._shared is not None:
            try:
                self._shared.touch()
            except RuntimeError:  # no running loop (sync caller): the local touch is enough
                pass

    def estimate_wait_s(self) -> float:
        shared = self._shared.estimate_wait_s() if self._shared is not None else 0.0
        return max(shared, self._local.estimate_wait_s())


_backends: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[Any, ...], LimiterBackend]] = (
    weakref.WeakKeyDictionary())


def reset_limiters() -> None:
    _backends.clear()


def get_limiter(secondary: bool = False) -> LimiterBackend:

    s = get_settings()
    client = bus.get_redis() if s.llm_limiter == "redis" else None
    table = _backends.setdefault(asyncio.get_running_loop(), {})
    key = (secondary, s.llm_limiter, id(client))
    if key in table:
        return table[key]
    local = LocalLimiterAdapter(secondary)
    backend: LimiterBackend = local
    if client is not None:
        try:
            ttl = max(s.llm_timeout_smart_s, s.llm_timeout_fast_s) + s.llm_timeout_cooldown_s + HOLD_MARGIN_S
            shared = SharedLimiter(client, "secondary" if secondary else "primary",
                                   s.llm_secondary_max_concurrency if secondary else s.llm_global_slots,
                                   s.llm_bg_max_slots, s.llm_user_max_slots, ttl)
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.limiter_setup_failed", error=type(exc).__name__)
            shared = None
        backend = FallingBackLimiter(shared, local)
    table[key] = backend
    return backend


class SharedProviderState:
    """Account-wide 429 backoff and timeout cooldown shared through Redis (spec 8.2). The local fields mirror
    the Redis values as of the last refresh()."""

    def __init__(self, client, provider: str) -> None:
        self._r, self._p = client, f"mavis:llm:{provider}:"
        self.backoff_until_ms = 0
        self.cooldown_until_ms = 0

    async def refresh(self) -> None:
        b, c = await self._r.mget(self._p + "backoff_until", self._p + "cooldown_until")
        self.backoff_until_ms, self.cooldown_until_ms = int(b or 0), int(c or 0)

    def backoff_s(self) -> float:
        return max(0.0, self.backoff_until_ms / 1000 - time.time())

    def unavailable_s(self) -> float:
        return max(self.backoff_until_ms, self.cooldown_until_ms) / 1000 - time.time()

    async def note_rate_limit_async(self, retry_after: float | None) -> float:
        level = int(await self._r.incr(self._p + "backoff_level"))
        await self._r.expire(self._p + "backoff_level", 300)
        steps = (5.0, 10.0, 20.0, 30.0)
        dur = retry_after if retry_after is not None else steps[min(level - 1, len(steps) - 1)]
        until = int((time.time() + dur) * 1000)
        await self._r.set(self._p + "backoff_until", until, px=int(dur * 1000) + 1000)
        self.backoff_until_ms = max(self.backoff_until_ms, until)
        return dur

    async def note_timeout_async(self, cooldown_s: float) -> None:
        until = int((time.time() + cooldown_s) * 1000)
        await self._r.set(self._p + "cooldown_until", until, px=int(cooldown_s * 1000) + 1000)
        self.cooldown_until_ms = max(self.cooldown_until_ms, until)

    async def note_success_async(self) -> None:
        await self._r.delete(self._p + "backoff_until", self._p + "backoff_level")
        self.backoff_until_ms = 0
