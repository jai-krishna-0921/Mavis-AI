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

from mavis.config import get_settings
from mavis.domain.errors import LLMError
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


# best_effort work yields: while an interactive or background caller waits, or one used the slot
# this recently, a best_effort acquire fails fast (LLMError) instead of taking the slot (one LEARN
# attempt holds the only slot for timeout + cooldown, 75s in prod). Callers drop the failure. A chat
# turn or a task makes several calls in a row with gaps between them, hence the grace window.
INTERACTIVE_GRACE_S = 20.0
BACKGROUND_AGING_S = 30.0  # a background waiter this old ranks with interactive (no starvation)
BACKGROUND_ACQUIRE_TIMEOUT_S = 600.0  # below BUS_CLAIM_IDLE_MS (15 min): never outlive a bus claim
INTERACTIVE_DEADLINE_S = 45.0  # chain-wide budget (queue + attempts + 429 backoffs) for a FAST reply
BACKGROUND_DEADLINE_S = 120.0  # same, for background calls and SMART-tier calls
_YIELDED = "LLM slot reserved for interactive work"


class _Waiter:
    __slots__ = ("fut", "rank", "since")

    def __init__(self, fut: asyncio.Future[None], rank: int, since: float) -> None:
        self.fut, self.rank, self.since = fut, rank, since


class _Limiter:
    """Process-wide cap on in-flight LLM calls (Ollama Cloud 429s on concurrent requests).

    A freed slot goes to the oldest waiter of the best rank: interactive, then background, then
    best_effort; a background waiter that has waited >= BACKGROUND_AGING_S ranks with interactive,
    so task work cannot starve behind chat. best_effort never ages and never waits behind or right
    after other work: while a higher-rank caller is queued or one held a slot within
    INTERACTIVE_GRACE_S, it may use spare slots but never the last free one (it fails fast with
    LLMError instead).
    A running call is never preempted. Everything here is synchronous (no await between state
    changes), so release() cannot be interrupted by a second cancellation.
    """

    def __init__(self, size: int) -> None:
        self._free = max(1, size)
        self._waiters: list[_Waiter] = []
        self._last_used = float("-inf")  # loop time a non-best_effort call last held a slot

    def touch(self) -> None:
        """A non-best_effort call is using (or just used) a slot: best_effort keeps off it a while."""
        self._last_used = asyncio.get_running_loop().time()

    def _work_active(self, now: float) -> bool:
        return (any(w.rank < 2 and not w.fut.done() for w in self._waiters)
                or now - self._last_used < INTERACTIVE_GRACE_S)

    def _best_effort_blocked(self, now: float) -> bool:
        """best_effort never takes the last free slot while higher-rank work is queued or recently ran:
        that slot is kept for the chat's next call. Spare slots beyond it may be used (with one slot,
        the last is the only one, so best_effort waits out the whole grace window)."""
        return self._free <= 1 and self._work_active(now)

    async def acquire(self, priority: Priority, wait_s: float) -> None:
        loop = asyncio.get_running_loop()
        rank = _RANK[priority]
        if rank == 2 and self._best_effort_blocked(loop.time()):
            raise LLMError(_YIELDED)
        if self._free > 0 and not self._waiters:
            self._free -= 1
            if rank < 2:
                self.touch()
            return
        waiter = _Waiter(loop.create_future(), rank, loop.time())
        self._waiters.append(waiter)
        self._dispatch()
        try:
            await asyncio.wait_for(waiter.fut, max(wait_s, 0.0))
        except BaseException as exc:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            elif waiter.fut.done() and not waiter.fut.cancelled() and waiter.fut.exception() is None:
                self.release()  # slot was handed over just as we gave up: pass it on
            if isinstance(exc, TimeoutError):
                raise LLMError("timed out waiting for an LLM slot") from exc
            raise

    def release(self) -> None:
        self._free += 1
        self._dispatch()

    def _dispatch(self) -> None:
        """Hand free slots to live waiters. Never raises; dead (cancelled/timed-out) waiters are dropped."""
        now = asyncio.get_running_loop().time()
        self._waiters = [w for w in self._waiters if not w.fut.done()]
        if self._best_effort_blocked(now):
            self._fail_best_effort()
        while self._free > 0 and self._waiters:
            pick = min(self._waiters, key=lambda w: (self._effective_rank(w, now), w.since))
            if pick.rank == 2 and self._best_effort_blocked(now):
                self._fail_best_effort()  # only the reserved slot is left
                continue
            self._waiters.remove(pick)
            if pick.fut.done():  # defensive: cancelled between the filter and here
                continue
            self._free -= 1
            if pick.rank < 2:
                self._last_used = now
            pick.fut.set_result(None)

    @staticmethod
    def _effective_rank(w: _Waiter, now: float) -> int:
        return 0 if w.rank == 1 and now - w.since >= BACKGROUND_AGING_S else w.rank

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
            await _await_backoff(priority, deadline)
        left = deadline.check()
        lim = _limiter(secondary)
        await lim.acquire(priority, left if priority == "interactive"
                          else min(left, BACKGROUND_ACQUIRE_TIMEOUT_S))
        hold = 0.0
        try:
            try:
                async with asyncio.timeout(deadline.check()):
                    result = await op()
            except TimeoutError as exc:
                if not secondary:
                    hold = _ollama.note_timeout()
                if deadline.remaining() <= 0:
                    raise LLMError("LLM deadline exceeded") from exc
                raise
            if not secondary:
                _ollama.note_success()
            return result
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, LLMError) or secondary:
                raise
            if _is_timeout(exc):
                hold = hold or _ollama.note_timeout()
                log.warning("llm.timeout_cooldown", hold_s=hold)
            elif _is_rate_limited(exc):
                dur = _ollama.note_rate_limit(_retry_after_s(exc))
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
                asyncio.get_running_loop().call_later(hold, lim.release)  # server still busy
            else:
                lim.release()
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
    """Interactive calls skip a saturated Ollama (cooldown / backoff) when a secondary exists."""
    return priority == "interactive" and _secondary_ready(tier) and _ollama.unavailable_s() > 0


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
