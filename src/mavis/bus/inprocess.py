"""Single-process bus for dev/tests: asyncio queues, in-memory dedupe, retry ≤5 then dead-letter list."""

from __future__ import annotations

import asyncio

import structlog

from mavis.bus.base import EventHandler, JobHandler
from mavis.domain.events import Event, Job

log = structlog.get_logger(__name__)


class InProcessBus:
    MAX_ATTEMPTS = 5

    def __init__(self) -> None:
        self._events: asyncio.Queue[tuple[Event, int]] = asyncio.Queue()
        self._jobs: asyncio.Queue[Job] = asyncio.Queue()
        self._seen: set[str] = set()
        self.dead_events: list[Event] = []
        self.dead_jobs: list[Job] = []
        self._closed = False

    async def publish(self, event: Event) -> bool:
        if event.id in self._seen:
            return False
        self._seen.add(event.id)
        await self._events.put((event, 0))
        return True

    async def enqueue(self, job: Job) -> None:
        await self._jobs.put(job)

    async def consume_events(self, group: str, consumer: str, handler: EventHandler) -> None:
        while not self._closed:
            event, attempts = await self._events.get()
            try:
                await handler(event)
            except Exception:
                log.exception("bus.event_failed", event_id=event.id, attempt=attempts + 1)
                if attempts + 1 >= self.MAX_ATTEMPTS:
                    self.dead_events.append(event)
                else:
                    self._events.put_nowait((event, attempts + 1))
            finally:
                self._events.task_done()

    async def consume_jobs(self, group: str, consumer: str, handler: JobHandler) -> None:
        while not self._closed:
            job = await self._jobs.get()
            try:
                await handler(job)
            except Exception:
                log.exception("bus.job_failed", job_id=job.id, kind=job.kind, attempt=job.attempts + 1)
                if job.attempts + 1 >= self.MAX_ATTEMPTS:
                    self.dead_jobs.append(job)
                else:
                    self._jobs.put_nowait(job.model_copy(update={"attempts": job.attempts + 1}))
            finally:
                self._jobs.task_done()

    async def wait_idle(self) -> None:
        """Block until both queues are drained, including work that publishes more work."""
        while True:
            await self._events.join()
            await self._jobs.join()
            # empty() is true while the last item is still being handled, so count unfinished work.
            if self._events._unfinished_tasks == 0 and self._jobs._unfinished_tasks == 0:  # noqa: SLF001
                return

    async def close(self) -> None:
        self._closed = True
