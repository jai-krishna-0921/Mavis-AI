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

from mavis.bus.base import SELF_RETRYING, EventBus, run_with_inline_retries
from mavis.channels import presence
from mavis.config import get_settings
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.domain.messages import Outbound
from mavis.store.repo import outbox, users
from mavis.worker.locks import lock, user_lock

log = structlog.get_logger(__name__)

EventFn = Callable[[Event], Awaitable[None]]
JobFn = Callable[[Job], Awaitable[None]]

WORKER_GROUP = "workers"
FALLBACK_TEXT = "Give me a sec, my brain is a bit slow right now. Try me again in a minute?"

CHAT_EVENT_TYPES = frozenset({EventType.USER_MESSAGE, EventType.BUTTON_PRESSED})

_ack_tasks: set[asyncio.Task] = set()  # strong refs so fire-and-forget acks are not GC'd

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


async def _run_handlers(event: Event, handlers: list[EventFn]) -> None:
    for fn in handlers:
        try:
            await fn(event)
        except LLMError:
            if event.trust is Trust.USER:
                # same dedupe_key on every attempt: the fallback is sent at most once per event
                await outbox.enqueue_now(
                    Outbound(user_id=event.user_id, text=FALLBACK_TEXT, dedupe_key=f"fallback:{event.id}")
                )
            raise


def _event_lock(event: Event):
    """Chat turns serialise on the user lock; initiative events on their own per-user key, so slow
    proactive LLM work never makes the user's next reply wait."""
    if event.type in CHAT_EVENT_TYPES:
        return user_lock(event.user_id)
    return lock(f"initiative:{event.user_id}", timeout_s=600)


async def _acknowledge(event: Event) -> None:
    """React to the user's Telegram message right away, before waiting on the user lock. Best effort."""
    message_id = event.payload.get("message_id")
    if event.type is not EventType.USER_MESSAGE or event.source != "telegram" or message_id is None:
        return
    try:
        user = await users.get(event.user_id)
        if user.telegram_chat_id is not None:
            await presence.react(user.telegram_chat_id, int(message_id))
    except Exception as exc:  # noqa: BLE001 - cosmetic, must not fail the turn
        log.warning("worker.ack_failed", error=type(exc).__name__)


async def handle_event(event: Event) -> None:
    handlers = list(_event_handlers.get(event.type, []))
    if not handlers:
        log.debug("worker.no_handler", event_type=event.type)
        return
    with structlog.contextvars.bound_contextvars(event_id=event.id, user_id=event.user_id):
        # Ack off the critical path: create_task does not run until we suspend, and _event_lock joins
        # the per-user FIFO before it suspends, so arrival order is kept and the reaction still goes
        # out right away, even while a previous turn holds the lock.
        ack = asyncio.create_task(_acknowledge(event))
        _ack_tasks.add(ack)
        ack.add_done_callback(_ack_tasks.discard)
        # The user lock is held across the inline retries (and their sleeps) so this user's next
        # event cannot overtake a retrying one. Other users run on the other consumer loops.
        async with _event_lock(event):
            await run_with_inline_retries(
                lambda: _run_handlers(event, handlers), what="event", ref=event.id
            )


setattr(handle_event, SELF_RETRYING, True)


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
