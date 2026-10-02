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


def _fallback_names(tier: Tier) -> list[str]:
    s = get_settings()
    fast = tier is Tier.FAST
    primary = s.model_fast if fast else s.model_smart
    names: list[str] = []
    for name in (s.model_fast_fallbacks if fast else s.model_smart_fallbacks):
        if name != primary and name not in names:
            names.append(name)
    return names


def _model_for(tier: Tier, temperature: float, name: str | None) -> BaseChatModel:
    # primary goes through the plain chat_model(tier, temperature) call so test fakes keep working
    return chat_model(tier, temperature) if name is None else chat_model(tier, temperature, model=name)


def _chain(tier: Tier) -> list[str | None]:
    return [None, *_fallback_names(tier)]


def _is_retriable(exc: BaseException) -> bool:
    """Timeouts, connection failures, 5xx and 429: another model may well succeed."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError,
                        openai.InternalServerError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


Priority = Literal["interactive", "background"]
RETRY_AFTER_CAP_S = 5.0  # a server Retry-After of 18s would make a reply crawl; fall back instead
RATE_LIMIT_BACKOFF_S = (0.5, 1.0, 2.0)  # same-model retries on 429 before moving to the next model


BACKGROUND_AGING_S = 30.0  # a background waiter this old is treated as interactive (no starvation)
BACKGROUND_ACQUIRE_TIMEOUT_S = 600.0  # below BUS_CLAIM_IDLE_MS (15 min): never outlive a bus claim
INTERACTIVE_DEADLINE_S = 25.0  # chain-wide budget (queue + attempts + 429 backoffs) for a FAST reply
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
        now = asyncio.get_running_loop().time()
        while self._free > 0 and self._waiters:
            pick = next(
                (w for w in self._waiters if w.interactive or now - w.since >= BACKGROUND_AGING_S),
                self._waiters[0],
            )
            self._waiters.remove(pick)
            self._free -= 1
            pick.fut.set_result(None)


# one limiter per event loop: asyncio primitives must not be shared across loops (pytest runs many)
_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Limiter] = weakref.WeakKeyDictionary()


def _limiter() -> _Limiter:
    loop = asyncio.get_running_loop()
    lim = _limiters.get(loop)
    if lim is None:
        lim = _limiters[loop] = _Limiter(get_settings().llm_max_concurrency)
    return lim


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


async def _call[R](op: Callable[[], Awaitable[R]], priority: Priority, deadline: _Deadline) -> R:
    """One model attempt under the limiter and the chain deadline; 429 backs off on the SAME model."""
    for delay in (*RATE_LIMIT_BACKOFF_S, None):
        left = deadline.check()
        lim = _limiter()
        await lim.acquire(priority, left if priority == "interactive"
                          else min(left, BACKGROUND_ACQUIRE_TIMEOUT_S))
        wait: float | None = None
        try:
            try:
                async with asyncio.timeout(deadline.check()):
                    return await op()
            except TimeoutError as exc:
                if deadline.remaining() <= 0:
                    raise LLMError("LLM deadline exceeded") from exc
                raise
        except Exception as exc:  # noqa: BLE001
            if delay is None or not _is_rate_limited(exc):
                raise
            wait = _retry_after_s(exc)
            log.warning("llm.rate_limited", retry_in_s=delay if wait is None else wait)
        finally:
            lim.release()
        # outside the slot so others can use it; never sleep past the deadline
        await _sleep(min(delay if wait is None else wait, max(deadline.remaining(), 0.0)))
    raise AssertionError("unreachable")  # pragma: no cover


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


async def complete(
    messages: list[BaseMessage],
    tier: Tier = Tier.FAST,
    temperature: float = 0.6,
    name: str = "complete",
    priority: Priority = "interactive",
) -> str:
    """Plain-text completion with model fallback. Raises LLMError on failure or empty output."""
    chain = _chain(tier)
    deadline = _deadline_for(tier, priority)
    out = None
    for i, model in enumerate(chain):
        try:
            llm = _model_for(tier, temperature, model)
            out = await _call(lambda m=llm: m.ainvoke(messages, config=run_config(name)), priority, deadline)
            break
        except Exception as exc:  # noqa: BLE001
            if _is_retriable(exc) and i + 1 < len(chain):
                _log_fallback(tier, model, chain[i + 1], exc)
                continue
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
) -> T:
    """Return a validated `schema` instance or raise LLMError.

    Per model: 1) native tool-calling structured output; 2) up to two JSON-mode attempts validated
    locally. Timeouts/connection/5xx/429 move on to the tier's next fallback model.
    """
    messages = _messages(system, user)
    chain = _chain(tier)
    deadline = _deadline_for(tier, priority)
    for i, model in enumerate(chain):
        try:
            return await _structured_with(model, schema, messages, tier, priority, deadline)
        except (_Unavailable, _Invalid) as failure:
            exc = failure.__cause__ or failure
            if i + 1 < len(chain):
                _log_fallback(tier, model, chain[i + 1], exc)
                continue
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
    deadline: _Deadline,
) -> T:
    cfg = run_config(f"structured:{schema.__name__}")
    try:
        runnable = _model_for(tier, 0.1, model).with_structured_output(schema, method="function_calling")
        result = await _call(lambda: runnable.ainvoke(messages, config=cfg), priority, deadline)
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
            llm = _model_for(tier, 0.1, model)
            raw = await _call(lambda m=llm: m.ainvoke(messages + [hint], config=cfg), priority, deadline)
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
