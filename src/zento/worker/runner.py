"""Event/job dispatch. Later phases register handlers; events per user are serialised.

Handlers must be idempotent: if any handler for an event raises, the bus retries the event and
every handler for it runs again. Use dedupe keys (outbox) / event_id (messages) / processed_events.
Jobs are NOT run under the user lock; a job handler that mutates conversation state takes it itself.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

import structlog

from zento.bus.base import EventBus
from zento.config import get_settings
from zento.domain.errors import LLMError
from zento.domain.events import Event, EventType, Job, JobKind, Trust
from zento.domain.messages import Outbound
from zento.store.repo import outbox
from zento.worker.locks import user_lock

log = structlog.get_logger(__name__)

EventFn = Callable[[Event], Awaitable[None]]
JobFn = Callable[[Job], Awaitable[None]]

WORKER_GROUP = "workers"
FALLBACK_TEXT = "Give me a sec, something's slow on my end. I'll get back to you on this."

_event_handlers: dict[EventType, list[EventFn]] = defaultdict(list)
_job_handlers: dict[JobKind, JobFn] = {}


def register_event_handler(event_type: EventType, fn: EventFn, *, replace: bool = False) -> None:
    if replace:
        _event_handlers[event_type] = []
    if fn not in _event_handlers[event_type]:
        _event_handlers[event_type].append(fn)


def register_job_handler(kind: JobKind, fn: JobFn) -> None:
    _job_handlers[kind] = fn


def clear_handlers() -> None:
    _event_handlers.clear()
    _job_handlers.clear()


async def handle_event(event: Event) -> None:
    handlers = list(_event_handlers.get(event.type, []))
    if not handlers:
        log.debug("worker.no_handler", event_type=event.type)
        return
    with structlog.contextvars.bound_contextvars(event_id=event.id, user_id=event.user_id):
        async with user_lock(event.user_id):
            for fn in handlers:
                try:
                    await fn(event)
                except LLMError:
                    if event.trust is Trust.USER:
                        await outbox.enqueue_now(
                            Outbound(
                                user_id=event.user_id,
                                text=FALLBACK_TEXT,
                                dedupe_key=f"fallback:{event.id}",
                            )
                        )
                    raise


async def handle_job(job: Job) -> None:
    fn = _job_handlers.get(job.kind)
    if fn is None:
        log.warning("worker.no_job_handler", kind=job.kind)
        return
    with structlog.contextvars.bound_contextvars(job_id=job.id, user_id=job.user_id, kind=job.kind.value):
        await fn(job)


async def run_worker(bus: EventBus, consumer: str, concurrency: int | None = None) -> None:
    """Consume events and jobs forever (until cancelled), `concurrency` loops per stream.

    Per-user event order holds because user_lock is FIFO within the process (see worker/locks.py).
    """
    n = max(1, concurrency or get_settings().worker_concurrency)
    loops = []
    for i in range(n):
        name = f"{consumer}-{i}"
        loops.append(bus.consume_events(WORKER_GROUP, name, handle_event))
        loops.append(bus.consume_jobs(WORKER_GROUP, name, handle_job))
    await asyncio.gather(*loops)
