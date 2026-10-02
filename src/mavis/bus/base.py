from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

import structlog

from mavis.domain.events import Event, Job

BLOCK_MS = 5_000  # XREADGROUP BLOCK; redis socket read timeout must exceed it

log = structlog.get_logger(__name__)

# Handler failures are retried in-process after these delays (seconds) BEFORE the message is left
# pending for the claim/DLQ path. Inline retries are not deliveries and never count toward the DLQ limit.
INLINE_RETRY_DELAYS_S: tuple[float, ...] = (2, 5, 10)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def run_with_inline_retries(call: Callable[[], Awaitable[None]], *, what: str, ref: str) -> None:
    """Run `call`; on Exception retry after each INLINE_RETRY_DELAYS_S delay, re-raising the last error.

    The caller's per-user lock must be taken inside `call`, so it is never held across a sleep.
    """
    for attempt, delay in enumerate((*INLINE_RETRY_DELAYS_S, None)):
        try:
            await call()
            return
        except Exception:
            if delay is None:
                raise
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
