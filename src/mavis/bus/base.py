from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

import httpx
import structlog
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from mavis.domain.errors import LLMError
from mavis.domain.events import Event, Job

BLOCK_MS = 5_000  # XREADGROUP BLOCK; redis socket read timeout must exceed it

log = structlog.get_logger(__name__)

# Handler failures are retried in-process after these delays (seconds) BEFORE the message is left
# pending for the claim/DLQ path. Inline retries are not deliveries and never count toward the DLQ limit.
INLINE_RETRY_DELAYS_S: tuple[float, ...] = (2, 5, 10)


def is_transient(exc: BaseException) -> bool:
    """Failures worth an in-process retry; anything else (bugs, bad data) goes straight to pending/DLQ."""
    return isinstance(
        exc, (LLMError, RedisTimeoutError, RedisConnectionError, TimeoutError, httpx.TransportError)
    )


# Marker for handlers that run `run_with_inline_retries` themselves (e.g. under the per-user lock);
# the bus then does not wrap them a second time.
SELF_RETRYING = "mavis_self_retrying"


def _llm_backoff_remaining() -> float:
    from mavis.llm import models  # local: keep the bus importable without the LLM stack

    return models._ollama.backoff_remaining()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def run_handler(handler: Callable[..., Awaitable[None]], item: object, *, what: str, ref: str) -> None:
    if getattr(handler, SELF_RETRYING, False):
        await handler(item)
    else:
        await run_with_inline_retries(lambda: handler(item), what=what, ref=ref)


async def run_with_inline_retries(call: Callable[[], Awaitable[None]], *, what: str, ref: str) -> None:
    """Run `call`, retrying transient errors after each INLINE_RETRY_DELAYS_S delay; re-raise the last.

    For events the per-user lock wraps this whole loop (see worker.runner.handle_event) so a user's
    next event cannot overtake a retrying one; jobs are unlocked.
    """
    for attempt, delay in enumerate((*INLINE_RETRY_DELAYS_S, None)):
        try:
            await call()
            return
        except Exception as exc:
            if delay is None or not is_transient(exc):
                raise
            # never retry into an active Ollama 429 backoff: that would just burn the attempt
            delay = max(delay, _llm_backoff_remaining())
            log.warning("bus.inline_retry", what=what, ref=ref, attempt=attempt + 1, retry_in_s=delay)
            await _sleep(delay)


class Stream(StrEnum):
    EVENTS = "mavis:events"
    JOBS = "mavis:jobs"


EventHandler = Callable[[Event], Awaitable[None]]
JobHandler = Callable[[Job], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, event: Event) -> bool:
        """Publish an event. Returns False if this event id was already seen (deduped)."""

    async def enqueue(self, job: Job) -> None: ...

    async def consume_events(self, group: str, consumer: str, handler: EventHandler) -> None:
        """Run forever, delivering events to handler; ack on success, retry ≤5 then DLQ."""

    async def consume_jobs(self, group: str, consumer: str, handler: JobHandler) -> None: ...

    async def close(self) -> None: ...
