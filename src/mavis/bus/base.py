from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from mavis.domain.events import Event, Job


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
