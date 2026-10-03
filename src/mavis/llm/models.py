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
from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

import httpx
import openai
import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
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


@lru_cache(maxsize=32)
def _build(model: str, base_url: str, api_key: str, temperature: float, timeout: float) -> ChatOpenAI:
    # max_retries=0: failover to the next model beats re-trying a dead one (each retry costs a full timeout).
    return ChatOpenAI(
        model=model, base_url=base_url, api_key=api_key or "missing",
        temperature=temperature, timeout=timeout, max_retries=0,
    )


def chat_model(tier: Tier = Tier.FAST, temperature: float = 0.6, model: str | None = None) -> BaseChatModel:
    s = get_settings()
    fast = tier is Tier.FAST
    return _build(
        model or (s.model_fast if fast else s.model_smart),
        s.ollama_base_url,
        s.ollama_api_key,
        temperature,
        s.llm_timeout_fast_s if fast else s.llm_timeout_smart_s,
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
    """Interactive calls walk the whole fallback chain. Background calls (LEARN, summaries,
    consolidation) make one model attempt, so a slow model never holds the single LLM slot for
    timeout x chain length; a 429 still backs off on that model and the bus retries the job.
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


Priority = Literal["interactive", "background"]
RETRY_AFTER_CAP_S = 30.0
# global backoff after a 429 without Retry-After; reset on the first success
RATE_LIMIT_BACKOFF_S = (5.0, 10.0, 20.0, 30.0)
MAX_ATTEMPTS = 6  # same-model attempts per call (timeouts / 429s), always bounded by the deadline


BACKGROUND_AGING_S = 30.0  # a background waiter this old is treated as interactive (no starvation)
BACKGROUND_ACQUIRE_TIMEOUT_S = 600.0  # below BUS_CLAIM_IDLE_MS (15 min): never outlive a bus claim
INTERACTIVE_DEADLINE_S = 45.0  # chain-wide budget (queue + attempts + 429 backoffs) for a FAST reply
BACKGROUND_DEADLINE_S = 120.0  # same, for background calls and SMART-tier calls


class _Waiter:
    __slots__ = ("fut", "interactive", "since")

    def __init__(self, fut: asyncio.Future[None], interactive: bool, since: float) -> None:
        self.fut, self.interactive, self.since = fut, interactive, since


class _Limiter:
    """Process-wide cap on in-flight LLM calls (Ollama Cloud 429s on concurrent requests).

    Two levels: a freed slot goes to the oldest *eligible* waiter, where eligible means interactive
    or a background waiter that has waited >= BACKGROUND_AGING_S. If none is eligible the oldest
    background waiter gets it. So replies never queue behind LEARN traffic, yet background work
    cannot starve. A running call is never preempted. Everything here is synchronous (no await
    between state changes), so release() cannot be interrupted by a second cancellation.
    """

    def __init__(self, size: int) -> None:
        self._free = max(1, size)
        self._waiters: list[_Waiter] = []

    async def acquire(self, priority: Priority, wait_s: float) -> None:
        loop = asyncio.get_running_loop()
        if self._free > 0 and not self._waiters:
            self._free -= 1
            return
        waiter = _Waiter(loop.create_future(), priority == "interactive", loop.time())
        self._waiters.append(waiter)
        self._dispatch()
        try:
            await asyncio.wait_for(waiter.fut, max(wait_s, 0.0))
        except BaseException as exc:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            elif waiter.fut.done() and not waiter.fut.cancelled():
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
        while self._free > 0 and self._waiters:
            pick = next(
                (w for w in self._waiters if w.interactive or now - w.since >= BACKGROUND_AGING_S),
                self._waiters[0],
            )
            self._waiters.remove(pick)
            if pick.fut.done():  # defensive: cancelled between the filter and here
                continue
            self._free -= 1
            pick.fut.set_result(None)


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
    """Global 429 backoff: everyone waits. Interactive callers only if the wait fits the deadline."""
    remaining = _ollama.backoff_remaining()
    if remaining <= 0:
        return
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
    `may_fail_over` (a secondary provider exists), in which case they are raised straight away.
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
            if may_fail_over:
                raise
            last = exc
        finally:
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
    chain = _chain(tier, _use_fallback(priority, fallback))
    out = None
    tried_secondary = False

    async def via_secondary() -> Any:
        llm = _model_for(tier, temperature, None, secondary=True)
        return await _call(lambda: llm.ainvoke(messages, config=run_config(name)), priority,
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
                llm = _model_for(tier, temperature, model)
                out = await _call(lambda m=llm: m.ainvoke(messages, config=run_config(name)), priority,
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
    text = _text_of(out.content).strip()
    if not text:
        raise LLMError(f"{tier.value} model returned empty content")
    return text


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
