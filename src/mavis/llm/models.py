"""Model routing (FAST / SMART) and validated structured output.

Callers MUST use `from mavis.llm import models as llm` and call `llm.structured(...)` etc. so tests can
monkeypatch this module. Internal calls go through the module-global `chat_model`, so patching it
also fakes `complete()` and the JSON fallback in `structured()`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

import httpx
import openai
import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from mavis import bus
from mavis.config import get_settings
from mavis.domain.errors import LLMError
from mavis.llm import policy
from mavis.llm.limiter import SharedProviderState, get_limiter, spawn
from mavis.llm.tracing import callbacks

log = structlog.get_logger(__name__)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class Tier(StrEnum):
    FAST = "fast"
    SMART = "smart"


REASONING_EFFORT_PREFIX = "gpt-oss"  # models that accept reasoning_effort


@lru_cache(maxsize=32)
def _build(model: str, base_url: str, api_key: str, temperature: float, timeout: float,
           reasoning_effort: str = "") -> ChatOpenAI:
    # max_retries=0: failover to the next model beats re-trying a dead one (each retry costs a full timeout).
    extra: dict[str, Any] = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    return ChatOpenAI(
        model=model, base_url=base_url, api_key=api_key or "missing",
        temperature=temperature, timeout=timeout, max_retries=0, **extra,
    )


def chat_model(tier: Tier = Tier.FAST, temperature: float = 0.6, model: str | None = None) -> BaseChatModel:
    s = get_settings()
    fast = tier is Tier.FAST
    name = model or (s.model_fast if fast else s.model_smart)
    return _build(
        name,
        s.ollama_base_url,
        s.ollama_api_key,
        temperature,
        s.llm_timeout_fast_s if fast else s.llm_timeout_smart_s,
        # gpt-oss emits ~60% fewer tokens at "low": shorter calls hold the single slot for less time.
        # Only gpt-oss takes it; other models (gemma fallback) may reject an unknown parameter.
        s.llm_reasoning_effort_fast if fast and name.startswith(REASONING_EFFORT_PREFIX) else "",
    )


def secondary_chat_model(tier: Tier = Tier.FAST, temperature: float = 0.6) -> BaseChatModel:
    """Chat model on the optional secondary OpenAI-compatible provider (call only if configured)."""
    s = get_settings()
    fast = tier is Tier.FAST
    return _build(
        s.llm_secondary_model_fast if fast else s.llm_secondary_model_smart,
        s.llm_secondary_base_url,
        s.llm_secondary_api_key,
        temperature,
        s.llm_timeout_fast_s if fast else s.llm_timeout_smart_s,
    )


def _secondary_ready(tier: Tier) -> bool:
    s = get_settings()
    model = s.llm_secondary_model_fast if tier is Tier.FAST else s.llm_secondary_model_smart
    return bool(s.llm_secondary_base_url and model)


def _fallback_names(tier: Tier) -> list[str]:
    s = get_settings()
    fast = tier is Tier.FAST
    primary = s.model_fast if fast else s.model_smart
    names: list[str] = []
    for name in (s.model_fast_fallbacks if fast else s.model_smart_fallbacks):
        if name != primary and name not in names:
            names.append(name)
    return names


def _model_for(tier: Tier, temperature: float, name: str | None, secondary: bool = False) -> BaseChatModel:
    if secondary:
        return secondary_chat_model(tier, temperature)
    # primary goes through the plain chat_model(tier, temperature) call so test fakes keep working
    return chat_model(tier, temperature) if name is None else chat_model(tier, temperature, model=name)


def _chain(tier: Tier, fallback: bool = True) -> list[str | None]:
    return [None, *_fallback_names(tier)] if fallback else [None]


def _use_fallback(priority: Priority, fallback: bool | None) -> bool:
    """Interactive calls walk the whole fallback chain. Background and best_effort calls make one
    model attempt, so a slow model never holds the single LLM slot for timeout x chain length.
    Pass `fallback=True` to opt a user-visible background call back in."""
    return priority == "interactive" if fallback is None else fallback


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, httpx.TimeoutException, openai.APITimeoutError))


def _is_connection_error(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TransportError, openai.APIConnectionError)) and not _is_timeout(exc)


def _is_provider_down(exc: BaseException) -> bool:
    """Timeout, 429 or connection failure: the PROVIDER (account) is saturated or unreachable, so
    another model on the same account cannot help. Only a secondary provider can."""
    return _is_timeout(exc) or _is_rate_limited(exc) or _is_connection_error(exc)


def _is_model_specific(exc: BaseException) -> bool:
    """404 model not found or 5xx from this model: the next model in the chain may well work."""
    if isinstance(exc, (openai.NotFoundError, openai.InternalServerError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 404 or status >= 500)


def _is_retriable(exc: BaseException) -> bool:
    return _is_provider_down(exc) or _is_model_specific(exc)


# interactive: a chat reply. background: user-visible work off the reply path (task runs, initiative
# reasoning/composing, attention understanding). best_effort: work that may simply be skipped (memory
# extraction, summaries, consolidation): it yields the slot to everything else and is never retried.
Priority = Literal["interactive", "background", "best_effort"]
_RANK: dict[str, int] = {"interactive": 0, "background": 1, "best_effort": 2}
RETRY_AFTER_CAP_S = 30.0
# global backoff after a 429 without Retry-After; reset on the first success
RATE_LIMIT_BACKOFF_S = (5.0, 10.0, 20.0, 30.0)
MAX_ATTEMPTS = 6  # same-model attempts per call (timeouts / 429s), always bounded by the deadline


# best_effort work (memory extraction, summaries, consolidation) must not cost a chat reply latency, but it
# must also run: it is the only way the user's facts, loops and profile get learned. The rules:
#   * with several slots, best_effort never takes the LAST free slot (it keeps one for the chat's next
#     call) and uses at most size // BEST_EFFORT_SHARE slots at once (min 1); otherwise it runs at once;
#   * when it cannot start it QUEUES (lowest rank, bounded by the call deadline) instead of failing;
#   * a best_effort waiter older than BEST_EFFORT_AGING_S ranks with background work among the waiters; a
#     timer re-checks queued waiters every BEST_EFFORT_TICK_S because "quiet" is about elapsed time, which
#     no acquire/release announces. Under sustained load it waits (deadline-bounded) and the caller's
#     durable job retries: LEARN is delayed, a reply never is (beyond a call already running when the
#     reply arrives, which only happens when it arrives within a lull-started call);
#   * with ONE slot (dev, small plans) there is nothing to share: it fails fast while any other work is
#     queued or ran within INTERACTIVE_GRACE_S, and the caller's job layer retries it later.
# A chat turn or a task makes several calls in a row with gaps between them (tool waits): the slot is held
# only while a call is in flight, never across the gap.
INTERACTIVE_GRACE_S = 20.0  # single-slot only: how long after other work best_effort keeps off the slot
BEST_EFFORT_SHARE = 3  # best_effort may use at most size // BEST_EFFORT_SHARE slots (min 1)
BEST_EFFORT_AGING_S = 45.0
BEST_EFFORT_QUIET_S = 5.0
BEST_EFFORT_TICK_S = 1.0
BACKGROUND_AGING_S = 30.0  # a background waiter this old ranks with interactive (no starvation)
BACKGROUND_ACQUIRE_TIMEOUT_S = 600.0  # below BUS_CLAIM_IDLE_MS (15 min): never outlive a bus claim
INTERACTIVE_DEADLINE_S = 45.0  # chain-wide budget (queue + attempts + 429 backoffs) for a FAST reply
BACKGROUND_DEADLINE_S = 120.0  # same, for background calls and SMART-tier calls
_YIELDED = "LLM slot reserved for interactive work"


def timings() -> policy.Timings:
    """The policy constants as they are now (tests move the module globals)."""
    return policy.Timings(BEST_EFFORT_SHARE, INTERACTIVE_GRACE_S, BEST_EFFORT_QUIET_S, BACKGROUND_AGING_S,
                          BEST_EFFORT_AGING_S)


class _Waiter:
    __slots__ = ("fut", "rank", "since")

    def __init__(self, fut: asyncio.Future[None], rank: int, since: float) -> None:
        self.fut, self.rank, self.since = fut, rank, since


class _Limiter:
    """Process-wide cap on in-flight LLM calls (Ollama Cloud 429s on concurrent requests).

    A freed slot goes to the oldest waiter of the best rank: interactive, then background, then
    best_effort; a background waiter that has waited >= BACKGROUND_AGING_S ranks with interactive,
    so task work cannot starve behind chat, and a best_effort waiter that has waited
    >= BEST_EFFORT_AGING_S ranks with background. See the rules above for best_effort.
    A running call is never preempted. Everything here is synchronous (no await between state
    changes), so release() cannot be interrupted by a second cancellation.
    """

    def __init__(self, size: int) -> None:
        self._size = max(1, size)
        self._free = self._size
        self._waiters: list[_Waiter] = []
        self._last_used = float("-inf")  # loop time a non-best_effort call last held a slot
        self._be_inflight = 0
        self._tick: asyncio.TimerHandle | None = None

    def touch(self) -> None:
        """A non-best_effort call used a slot just now (with one slot, best_effort keeps off a while)."""
        self._last_used = asyncio.get_running_loop().time()

    def _snapshot(self, now: float) -> policy.Snapshot:
        return policy.Snapshot(self._size, self._free, self._be_inflight, self._higher_waiting(),
                               now - self._last_used)

    def _single_slot_blocked(self, now: float) -> bool:
        return policy.single_slot_blocked(self._snapshot(now), timings())

    def _best_effort_may_start(self, now: float, since: float | None = None) -> bool:
        """`since`: when this caller started waiting; it only ranks it (see _effective_rank)."""
        return policy.best_effort_may_start(self._snapshot(now), timings())

    def _take(self, rank: int, now: float) -> None:
        self._free -= 1
        if rank == 2:
            self._be_inflight += 1
        else:
            self._last_used = now

    def _higher_waiting(self) -> bool:
        return any(w.rank < 2 and not w.fut.done() for w in self._waiters)

    async def acquire(self, priority: Priority, wait_s: float) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        rank = _RANK[priority]
        if rank == 2:
            if self._single_slot_blocked(now):
                raise LLMError(_YIELDED)
            if not self._waiters and self._best_effort_may_start(now):
                self._take(rank, now)
                return
        elif self._free > 0 and not self._higher_waiting():
            # best_effort waiters never delay a higher rank: it takes the slot ahead of them
            self._take(rank, now)
            return
        waiter = _Waiter(loop.create_future(), rank, now)
        self._waiters.append(waiter)
        self._dispatch()
        if rank == 2:
            self._arm_tick()
        try:
            await asyncio.wait_for(waiter.fut, max(wait_s, 0.0))
        except BaseException as exc:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            elif waiter.fut.done() and not waiter.fut.cancelled() and waiter.fut.exception() is None:
                self.release(best_effort=rank == 2)  # slot was handed over just as we gave up: pass it on
            if isinstance(exc, TimeoutError):
                raise LLMError("timed out waiting for an LLM slot") from exc
            raise

    def _arm_tick(self) -> None:
        """Re-dispatch on a timer while best_effort callers wait: ageing and the quiet period are about
        elapsed time, which no acquire/release event announces."""
        if self._tick is not None or not any(w.rank == 2 and not w.fut.done() for w in self._waiters):
            return
        self._tick = asyncio.get_running_loop().call_later(BEST_EFFORT_TICK_S, self._on_tick)

    def _on_tick(self) -> None:
        self._tick = None
        self._dispatch()
        self._arm_tick()

    def release(self, best_effort: bool = False) -> None:
        self._free += 1
        if best_effort:
            self._be_inflight = max(0, self._be_inflight - 1)
        self._dispatch()

    def _dispatch(self) -> None:
        """Hand free slots to live waiters. Never raises; dead (cancelled/timed-out) waiters are dropped."""
        now = asyncio.get_running_loop().time()
        self._waiters = [w for w in self._waiters if not w.fut.done()]
        if self._single_slot_blocked(now):
            self._fail_best_effort()
        while self._free > 0 and self._waiters:
            ranked = sorted(self._waiters, key=lambda w: (self._effective_rank(w, now), w.since))
            pick = next((w for w in ranked if w.rank < 2 or self._best_effort_may_start(now, w.since)), None)
            if pick is None:
                break  # only best_effort waiters are left and none may start yet
            self._waiters.remove(pick)
            if pick.fut.done():  # defensive: cancelled between the filter and here
                continue
            self._take(pick.rank, now)
            pick.fut.set_result(None)

    @staticmethod
    def _effective_rank(w: _Waiter, now: float) -> int:
        return policy.effective_rank(w.rank, now - w.since, timings())

    def _fail_best_effort(self) -> None:
        for w in [w for w in self._waiters if w.rank == 2]:
            self._waiters.remove(w)
            if not w.fut.done():
                w.fut.set_exception(LLMError(_YIELDED))


# one limiter per event loop and provider: asyncio primitives must not be shared across loops
_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Limiter] = weakref.WeakKeyDictionary()
_secondary_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Limiter] = (
    weakref.WeakKeyDictionary()
)


def _limiter(secondary: bool = False) -> _Limiter:
    loop = asyncio.get_running_loop()
    table = _secondary_limiters if secondary else _limiters
    lim = table.get(loop)
    if lim is None:
        s = get_settings()
        lim = table[loop] = _Limiter(s.llm_secondary_max_concurrency if secondary else s.llm_max_concurrency)
    return lim


class _OllamaState:
    """Process-wide view of the Ollama account: global 429 backoff and timeout cooldown."""

    def __init__(self) -> None:
        self.backoff_until = 0.0
        self.level = 0
        self.cooldown_until = 0.0

    def reset(self) -> None:
        self.backoff_until = self.cooldown_until = 0.0
        self.level = 0

    def backoff_remaining(self) -> float:
        return max(0.0, self.backoff_until - time.monotonic())

    def unavailable_s(self) -> float:
        return max(self.backoff_until, self.cooldown_until) - time.monotonic()

    def note_success(self) -> None:
        self.backoff_until = 0.0
        self.level = 0

    def note_rate_limit(self, retry_after: float | None) -> float:
        if retry_after is None:
            dur = RATE_LIMIT_BACKOFF_S[min(self.level, len(RATE_LIMIT_BACKOFF_S) - 1)]
            self.level += 1
        else:
            dur = retry_after
        self.backoff_until = max(self.backoff_until, time.monotonic() + dur)
        return dur

    def note_timeout(self) -> float:
        cooldown = get_settings().llm_timeout_cooldown_s
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + cooldown)
        return cooldown


_ollama = _OllamaState()


def unavailable_s() -> float:
    """Seconds until the primary provider is usable again (429 backoff or timeout cooldown); <= 0 if ready.

    Background work (the attention drain) checks this before starting a call, so it never queues
    behind a saturated account while a chat reply might need the slot."""
    return _ollama.unavailable_s()


def _is_rate_limited(exc: BaseException) -> bool:
    return (
        isinstance(exc, openai.RateLimitError)
        or getattr(exc, "status_code", None) == 429
        or "too many concurrent requests" in str(exc).lower()
    )


def _retry_after_s(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        return min(RETRY_AFTER_CAP_S, max(0.0, float(headers.get("retry-after"))))
    except (TypeError, ValueError):
        return None


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def check(self) -> float:
        left = self.remaining()
        if left <= 0:
            raise LLMError("LLM deadline exceeded")
        return left


def _deadline_for(tier: Tier, priority: Priority) -> _Deadline:
    fast_reply = tier is Tier.FAST and priority == "interactive"
    return _Deadline(INTERACTIVE_DEADLINE_S if fast_reply else BACKGROUND_DEADLINE_S)


def _shared_state():
    """The Redis-backed provider state when LLM_LIMITER=redis and Redis is configured, else None."""
    if get_settings().llm_limiter != "redis" or (client := bus.get_redis()) is None:
        return None
    cached = _shared_cache.get("primary")
    if cached is None or cached[0] is not client:
        cached = _shared_cache["primary"] = (client, SharedProviderState(client, "primary"))
    return cached[1]


_shared_cache: dict[str, tuple[Any, SharedProviderState]] = {}


async def _refresh_shared_state() -> None:
    """Copy the account-wide backoff / cooldown other processes recorded into this process's view."""
    state = _shared_state()
    if state is None:
        return
    try:
        await state.refresh()
    except Exception as exc:  # noqa: BLE001 - Redis trouble: the local view still works
        log.debug("llm.shared_state_refresh_failed", error=type(exc).__name__)
        return
    now = time.monotonic()
    if (back := state.backoff_s()) > 0:
        _ollama.backoff_until = max(_ollama.backoff_until, now + back)
    if (cool := state.cooldown_until_ms / 1000 - time.time()) > 0:
        _ollama.cooldown_until = max(_ollama.cooldown_until, now + cool)


def _shared_note(kind: str, value: float | None = None) -> None:
    """Record a provider event in Redis without delaying the call (fire and forget)."""
    state = _shared_state()
    if state is None:
        return
    if kind == "rate":
        coro = state.note_rate_limit_async(value)
    elif kind == "timeout":
        coro = state.note_timeout_async(value or 0.0)
    elif state.backoff_until_ms:
        coro = state.note_success_async()
    else:
        return
    spawn(coro)


async def _await_backoff(priority: Priority, deadline: _Deadline) -> None:
    """Global 429 backoff: everyone waits. Interactive callers only if the wait fits the deadline;
    best_effort callers never wait (their work is dropped, the account is saturated)."""
    remaining = _ollama.backoff_remaining()
    if remaining <= 0:
        return
    if priority == "best_effort":
        raise LLMError("LLM rate-limit backoff active")
    left = deadline.check()
    if priority == "interactive" and remaining >= left:
        raise LLMError("LLM rate-limit backoff outlasts the deadline")
    await _sleep(min(remaining, left))


async def _call[R](
    op: Callable[[], Awaitable[R]], priority: Priority, deadline: _Deadline,
    *, secondary: bool = False, may_fail_over: bool = False,
) -> R:
    """One model attempt under the provider's limiter and the deadline.

    Ollama: after a client timeout the slot stays occupied for the cooldown (the abandoned request
    still runs server-side); a 429 sets the process-wide backoff. Both retry the SAME model, unless
    `may_fail_over` (a secondary provider exists) or the call is best_effort, in which case they
    are raised straight away.
    """
    last: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        deadline.check()
        if not secondary:
            await _refresh_shared_state()
            await _await_backoff(priority, deadline)
        left = deadline.check()
        lim = get_limiter(secondary)
        lease = await lim.acquire(priority, left if priority == "interactive"
                                  else min(left, BACKGROUND_ACQUIRE_TIMEOUT_S))
        hold = 0.0
        try:
            try:
                async with asyncio.timeout(deadline.check()):
                    result = await op()
            except TimeoutError as exc:
                if not secondary:
                    hold = _ollama.note_timeout()
                    _shared_note("timeout", hold)
                if deadline.remaining() <= 0:
                    raise LLMError("LLM deadline exceeded") from exc
                raise
            if not secondary:
                _ollama.note_success()
                _shared_note("ok")
            return result
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, LLMError) or secondary:
                raise
            if _is_timeout(exc):
                hold = hold or _ollama.note_timeout()
                _shared_note("timeout", hold)
                log.warning("llm.timeout_cooldown", hold_s=hold)
            elif _is_rate_limited(exc):
                retry_after = _retry_after_s(exc)
                dur = _ollama.note_rate_limit(retry_after)
                _shared_note("rate", retry_after)
                log.warning("llm.rate_limited", backoff_s=dur)
            else:
                raise
            if may_fail_over or priority == "best_effort":
                raise
            last = exc
        finally:
            if priority != "best_effort":
                lim.touch()  # the grace window runs from the END of a chat or task call
            if hold > 0:
                asyncio.get_running_loop().call_later(hold, lim.release, lease)  # server still busy
            else:
                lim.release(lease)
    assert last is not None
    raise last


def _log_fallback(tier: Tier, frm: str | None, to: str | None, exc: BaseException) -> None:
    s = get_settings()
    primary = s.model_fast if tier is Tier.FAST else s.model_smart
    log.warning("llm.fallback", tier=tier.value, from_model=frm or primary, to_model=to,
                error=type(exc).__name__)


def run_config(name: str) -> dict[str, Any]:
    return {"callbacks": callbacks(), "run_name": name}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)


def _prefer_secondary(tier: Tier, priority: Priority) -> bool:
    """Overflow routing (spec 8.4). Interactive: skip a saturated or backed-up primary when a secondary
    exists. Background: only after a long primary backoff. best_effort never overflows."""
    if not _secondary_ready(tier) or priority == "best_effort":
        return False
    s = get_settings()
    if priority == "interactive":
        return _ollama.unavailable_s() > 0 or get_limiter(False).estimate_wait_s() > s.llm_overflow_wait_s
    return _ollama.unavailable_s() > s.llm_bg_overflow_after_s


async def _invoke_chain(
    messages: list[BaseMessage],
    tier: Tier,
    temperature: float,
    name: str,
    priority: Priority,
    fallback: bool | None,
    prepare: Callable[[BaseChatModel], Any],
) -> Any:
    """Shared model chain for complete() and invoke_tools(): one runnable per model, built by
    `prepare(chat_model)` (identity, or `.bind_tools(...)`), each attempt under `_call` (limiter,
    deadline, Ollama cooldown/backoff). Falls back to another model only on model-specific errors,
    and once to the secondary provider when Ollama is saturated or unreachable. Raises LLMError."""
    chain = _chain(tier, _use_fallback(priority, fallback))
    cfg = run_config(name)
    out = None
    tried_secondary = False

    async def via_secondary() -> Any:
        runnable = prepare(_model_for(tier, temperature, None, secondary=True))
        return await _call(lambda: runnable.ainvoke(messages, config=cfg), priority,
                           _deadline_for(tier, priority), secondary=True)

    if _prefer_secondary(tier, priority):
        tried_secondary = True
        try:
            out = await via_secondary()
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.secondary_failed", error=type(exc).__name__)
    if out is None:
        deadline = _deadline_for(tier, priority)
        for i, model in enumerate(chain):
            try:
                runnable = prepare(_model_for(tier, temperature, model))
                out = await _call(lambda r=runnable: r.ainvoke(messages, config=cfg), priority,
                                  deadline, may_fail_over=_secondary_ready(tier) and not tried_secondary)
                break
            except Exception as exc:  # noqa: BLE001
                if _is_model_specific(exc) and i + 1 < len(chain):
                    _log_fallback(tier, model, chain[i + 1], exc)
                    continue
                if _is_provider_down(exc) and _secondary_ready(tier) and not tried_secondary:
                    tried_secondary = True
                    log.warning("llm.secondary_fallback", tier=tier.value, error=type(exc).__name__)
                    try:
                        out = await via_secondary()
                        break
                    except Exception as exc2:  # noqa: BLE001
                        raise LLMError(f"{tier.value} model call failed: {type(exc2).__name__}") from exc2
                raise LLMError(f"{tier.value} model call failed: {type(exc).__name__}") from exc
    assert out is not None
    return out


async def complete(
    messages: list[BaseMessage],
    tier: Tier = Tier.FAST,
    temperature: float = 0.6,
    name: str = "complete",
    priority: Priority = "interactive",
    fallback: bool | None = None,
) -> str:
    """Plain-text completion. Falls back to another model only on model-specific errors, and once to
    the secondary provider when Ollama is saturated or unreachable. Raises LLMError on failure."""
    out = await _invoke_chain(messages, tier, temperature, name, priority, fallback, lambda m: m)
    text = _text_of(out.content).strip()
    if not text:
        raise LLMError(f"{tier.value} model returned empty content")
    return text


async def invoke_tools(
    messages: list[BaseMessage],
    tools: Sequence[Any],
    tier: Tier = Tier.FAST,
    temperature: float = 0.3,
    name: str = "tools",
    priority: Priority = "interactive",
    fallback: bool | None = None,
) -> AIMessage:
    """One tool-calling turn: the model with `tools` bound, through the same chain as complete()
    (limiter, deadline, cooldown/backoff, secondary provider). This is the ONLY way to make a
    tool-calling LLM call. Empty content is fine when the model asked for tools (valid or not);
    otherwise it raises LLMError like complete()."""
    bound = list(tools)
    out = await _invoke_chain(messages, tier, temperature, name, priority, fallback,
                              lambda m: m.bind_tools(bound) if bound else m)
    if not isinstance(out, AIMessage):
        raise LLMError(f"{tier.value} model returned {type(out).__name__}, not an AIMessage")
    if not (out.tool_calls or out.invalid_tool_calls) and not _text_of(out.content).strip():
        raise LLMError(f"{tier.value} model returned empty content")
    return out


def _messages(system: str, user: str | list[BaseMessage]) -> list[BaseMessage]:
    head: list[BaseMessage] = [SystemMessage(system)]
    return head + ([HumanMessage(user)] if isinstance(user, str) else list(user))


async def structured[T: BaseModel](
    schema: type[T],
    system: str,
    user: str | list[BaseMessage],
    tier: Tier = Tier.FAST,
    priority: Priority = "interactive",
    fallback: bool | None = None,
) -> T:
    """Return a validated `schema` instance or raise LLMError.

    Per model: 1) native tool-calling structured output; 2) up to two JSON-mode attempts validated
    locally. Only model-specific errors (404, 5xx) or invalid output move to the tier's next model;
    timeouts/429/connection errors retry the same model after cooldown/backoff, or go once to the
    secondary provider if configured (same tool-calling / JSON-mode path via ChatOpenAI).
    """
    messages = _messages(system, user)
    chain = _chain(tier, _use_fallback(priority, fallback))
    tried_secondary = False

    async def via_secondary() -> T:
        return await _structured_with(None, schema, messages, tier, priority,
                                      _deadline_for(tier, priority), secondary=True)

    if _prefer_secondary(tier, priority):
        tried_secondary = True
        try:
            return await via_secondary()
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.secondary_failed", error=type(exc).__name__)
    deadline = _deadline_for(tier, priority)
    for i, model in enumerate(chain):
        try:
            return await _structured_with(model, schema, messages, tier, priority, deadline,
                                          may_fail_over=_secondary_ready(tier) and not tried_secondary)
        except (_Unavailable, _Invalid) as failure:
            exc = failure.__cause__ or failure
            if i + 1 < len(chain) and (isinstance(failure, _Invalid) or _is_model_specific(exc)):
                _log_fallback(tier, model, chain[i + 1], exc)
                continue
            if isinstance(failure, _Unavailable) and _is_provider_down(exc) \
                    and _secondary_ready(tier) and not tried_secondary:
                log.warning("llm.secondary_fallback", tier=tier.value, error=type(exc).__name__)
                try:
                    return await via_secondary()
                except (_Unavailable, _Invalid) as f2:
                    e2 = f2.__cause__ or f2
                    raise LLMError(f"could not get a valid {schema.__name__}: {type(e2).__name__}") from e2
            if isinstance(failure, _Invalid):
                raise LLMError(str(failure)) from failure
            raise LLMError(f"could not get a valid {schema.__name__}: {type(exc).__name__}") from exc
    raise AssertionError("unreachable")  # pragma: no cover


class _Invalid(LLMError):
    """Tool mode and JSON mode both failed to give a valid object on this model; try the next one."""


class _Unavailable(Exception):
    """The model could not be reached (retriable); __cause__ holds the original error."""


async def _structured_with[T: BaseModel](
    model: str | None, schema: type[T], messages: list[BaseMessage], tier: Tier, priority: Priority,
    deadline: _Deadline, *, secondary: bool = False, may_fail_over: bool = False,
) -> T:
    cfg = run_config(f"structured:{schema.__name__}")
    try:
        runnable = _model_for(tier, 0.1, model, secondary).with_structured_output(
            schema, method="function_calling")
        result = await _call(lambda: runnable.ainvoke(messages, config=cfg), priority, deadline,
                             secondary=secondary, may_fail_over=may_fail_over)
        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            return schema.model_validate(result)
    except LLMError:
        raise  # deadline / queue timeout: no point trying anything else
    except Exception as exc:  # noqa: BLE001 - fall through to JSON mode
        if _is_retriable(exc):
            raise _Unavailable() from exc
        log.debug("llm.structured.tool_mode_failed", schema=schema.__name__, error=type(exc).__name__)

    hint = HumanMessage(
        "Respond with ONLY a JSON object matching this JSON schema, no prose:\n"
        + json.dumps(schema.model_json_schema())
    )
    last: Exception | None = None
    for _ in range(2):
        try:
            llm = _model_for(tier, 0.1, model, secondary)
            raw = await _call(lambda m=llm: m.ainvoke(messages + [hint], config=cfg), priority, deadline,
                              secondary=secondary, may_fail_over=may_fail_over)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_retriable(exc):
                raise _Unavailable() from exc
            last = exc
            continue
        content = _text_of(raw.content)
        match = _JSON_OBJECT.search(content)
        try:
            return schema.model_validate_json(match.group(0) if match else content)
        except ValidationError as exc:
            last = exc
    raise _Invalid(f"could not get a valid {schema.__name__}: {type(last).__name__ if last else 'unknown'}")
