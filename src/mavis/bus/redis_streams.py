"""Redis Streams bus: consumer groups, SET NX dedupe (7d), XAUTOCLAIM for stuck messages, DLQ stream."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from mavis.bus.base import EventHandler, JobHandler, Stream
from mavis.domain.events import Event, Job

log = structlog.get_logger(__name__)
MAXLEN = 100_000


class RedisStreamsBus:
    def __init__(
        self,
        client: Redis,
        *,
        claim_idle_ms: int = 60_000,
        block_ms: int = 5_000,
        dedupe_ttl_s: int = 7 * 24 * 3600,
        max_attempts: int = 5,
    ) -> None:
        self._r = client
        self._claim_idle_ms = claim_idle_ms
        self._block_ms = block_ms
        self._ttl = dedupe_ttl_s
        self._max = max_attempts
        self._closed = False

    async def publish(self, event: Event) -> bool:
        fresh = await self._r.set(f"mavis:seen:{event.id}", "1", nx=True, ex=self._ttl)
        if not fresh:
            return False
        try:
            await self._r.xadd(Stream.EVENTS.value, {"data": event.model_dump_json()}, maxlen=MAXLEN,
                               approximate=True)
        except BaseException:
            # release the dedupe key so a retry of this event is not mistaken for a duplicate
            await self._r.delete(f"mavis:seen:{event.id}")
            raise
        return True

    async def enqueue(self, job: Job) -> None:
        await self._r.xadd(Stream.JOBS.value, {"data": job.model_dump_json()}, maxlen=MAXLEN,
                           approximate=True)

    async def consume_events(self, group: str, consumer: str, handler: EventHandler) -> None:
        async def handle(data: str, attempts: int) -> None:
            await handler(Event.model_validate_json(data))

        await self._consume(Stream.EVENTS, group, consumer, handle)

    async def consume_jobs(self, group: str, consumer: str, handler: JobHandler) -> None:
        async def handle(data: str, attempts: int) -> None:
            job = Job.model_validate_json(data)
            await handler(job.model_copy(update={"attempts": attempts}))

        await self._consume(Stream.JOBS, group, consumer, handle)

    async def close(self) -> None:
        self._closed = True
        await self._r.aclose()

    # --- internals -------------------------------------------------------------
    async def _ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._r.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume(
        self, stream: Stream, group: str, consumer: str, handle: Callable[[str, int], Awaitable[None]]
    ) -> None:
        await self._ensure_group(stream.value, group)
        while not self._closed:
            try:
                entries = await self._claim_stale(stream.value, group, consumer)
                if not entries:
                    resp = await self._r.xreadgroup(group, consumer, {stream.value: ">"}, count=10,
                                                    block=self._block_ms)
                    entries = [entry for _name, items in (resp or []) for entry in items]
                for msg_id, fields in entries:
                    await self._process(stream.value, group, msg_id, fields, handle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closed:
                    return
                if "NOGROUP" in str(exc):  # Redis restarted without persistence: stream/group are gone
                    log.warning("bus.group_missing_recreating", stream=stream.value, group=group)
                    await self._ensure_group(stream.value, group)
                    continue
                log.exception("bus.consume_loop_error", stream=stream.value)
                await asyncio.sleep(1)

    async def _claim_stale(self, stream: str, group: str, consumer: str) -> list[tuple[str, dict[str, Any]]]:
        res = await self._r.xautoclaim(stream, group, consumer, min_idle_time=self._claim_idle_ms,
                                       start_id="0-0", count=10)
        messages = res[1] if len(res) > 1 else []
        return [(mid, fields) for mid, fields in messages if fields]

    async def _process(
        self, stream: str, group: str, msg_id: str, fields: dict[str, Any],
        handle: Callable[[str, int], Awaitable[None]],
    ) -> None:
        data = fields.get("data") or fields.get(b"data")
        if isinstance(data, bytes):
            data = data.decode()
        delivered = await self._delivery_count(stream, group, msg_id)
        if delivered > self._max:
            # delivered `max` times without ever completing (e.g. the worker keeps crashing)
            log.error("bus.delivery_limit_exceeded", stream=stream, msg_id=str(msg_id), delivered=delivered)
            await self._dead_letter(stream, group, msg_id, data)
            return
        try:
            await handle(data, delivered - 1)
        except Exception:
            log.exception("bus.handler_failed", stream=stream, msg_id=str(msg_id), attempt=delivered)
            if delivered >= self._max:
                await self._dead_letter(stream, group, msg_id, data)
            return  # unacked: XAUTOCLAIM redelivers after claim_idle_ms
        await self._r.xack(stream, group, msg_id)

    async def _delivery_count(self, stream: str, group: str, msg_id: str) -> int:
        """Redis' own delivery counter (XPENDING), which also counts deliveries that crashed."""
        rows = await self._r.xpending_range(stream, group, min=msg_id, max=msg_id, count=1)
        return int(rows[0]["times_delivered"]) if rows else 1

    async def _dead_letter(self, stream: str, group: str, msg_id: str, data: str) -> None:
        await self._r.xadd(f"{stream}:dlq", {"data": data, "msg_id": str(msg_id)})
        await self._r.xack(stream, group, msg_id)
