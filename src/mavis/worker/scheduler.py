"""Two-stage worker (spec 7): intake moves events from the stream into per-user mailboxes (fast, acks the
reaction), executors claim one user at a time per lane and run the gates and handlers exactly as
handle_event does (inline retries, processed_events dedupe)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import structlog

from mavis.bus.base import run_with_inline_retries
from mavis.config import get_settings
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Role
from mavis.llm.context import bind_user
from mavis.store.repo import messages, users
from mavis.worker import gates, runner
from mavis.worker.mailbox import Lane, MailboxBackend

log = structlog.get_logger(__name__)
MAX_TURN_FAILURES = 3  # failed turns of one event before it is dropped


def lane_of(event: Event) -> Lane:
    return "chat" if event.type in runner.CHAT_EVENT_TYPES else "bg"


def coalesce(events: list[Event]) -> tuple[list[Event], Event]:
    s = get_settings()
    first = events[0]
    if first.type is not EventType.USER_MESSAGE or s.coalesce_window_s <= 0:
        return [], first
    run = [first]
    chars = len(str(first.payload.get("text", "")))
    for e in events[1:]:
        gap = (e.occurred_at - run[-1].occurred_at).total_seconds()
        size = len(str(e.payload.get("text", "")))
        if (e.type is not EventType.USER_MESSAGE or gap > s.coalesce_window_s
                or len(run) >= s.coalesce_max_messages or chars + size > s.coalesce_max_chars):
            break
        run.append(e)
        chars += size
    return run[:-1], run[-1]


class Scheduler:
    def __init__(self, backend: MailboxBackend, *, chat_executors: int | None = None,
                 bg_executors: int | None = None, coalesce_window_s: float | None = None,
                 reap_on_idle: bool = True) -> None:
        s = get_settings()
        self._box = backend
        self._n = {"chat": chat_executors or s.chat_executors, "bg": bg_executors or s.bg_executors}
        self._window = s.coalesce_window_s if coalesce_window_s is None else coalesce_window_s
        self._wake = {"chat": asyncio.Event(), "bg": asyncio.Event()}
        self._failures: dict[str, int] = {}
        self._reap_on_idle = reap_on_idle  # the in-process mailbox has no timer-role reaper

    async def intake(self, event: Event) -> None:
        if event.type is EventType.USER_MESSAGE:
            ack = asyncio.create_task(runner._acknowledge(event))
            runner._ack_tasks.add(ack)
            ack.add_done_callback(runner._ack_tasks.discard)
        lane = lane_of(event)
        await self._box.push(event.user_id, lane, event.model_dump_json())
        self._wake[lane].set()

    async def _run_one(self, lane: Lane, owner: str) -> bool:
        claimed = await self._box.claim(lane, owner)
        if claimed is None:
            return False
        uid, raw = claimed
        events = [Event.model_validate_json(r) for r in raw]
        earlier, last = await self._plan(events)
        heartbeat = asyncio.create_task(self._heartbeat(uid, lane, owner))
        try:
            for e in earlier:  # Deviation 4: each message keeps its own row; the turn reads them from history
                await messages.log(e.user_id, Role.USER, str(e.payload.get("text", "")), event_id=e.id)
            await self._handle(last)
            await self._box.commit(uid, lane, len(earlier) + 1)
        except Exception:  # noqa: BLE001 - leave the entries; the lease expires and the reaper retries
            log.exception("scheduler.turn_failed", user_id=uid, lane=lane)
            n = self._failures[last.id] = self._failures.get(last.id, 0) + 1
            if n >= MAX_TURN_FAILURES:  # a poison event must not hold the user's mailbox forever
                log.error("scheduler.poison_dropped", event_id=last.id, user_id=uid)
                self._failures.pop(last.id, None)
                await self._box.commit(uid, lane, len(earlier) + 1)
                await self._box.release(uid, lane, owner)
            return True
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await self._box.release(uid, lane, owner)
        return True

    async def _plan(self, events: list[Event]) -> tuple[list[Event], Event]:
        """Which leading messages fold into one turn. Only an active user's plain messages coalesce: a
        pending or banned user's content is never stored, and the gates must see each of their events."""
        first = events[0]
        if self._window <= 0 or first.type is not EventType.USER_MESSAGE or first.payload.get("pending"):
            return [], first
        if (await users.get(events[0].user_id)).status != "active":
            return [], events[0]
        return coalesce(events)

    async def _handle(self, event: Event) -> None:
        handlers = list(runner._event_handlers.get(event.type, []))

        async def attempt() -> None:
            with bind_user(event.user_id, runner._purpose(event)):
                if await gates.run_gates(event):
                    await runner._run_handlers(event, handlers)

        with structlog.contextvars.bound_contextvars(event_id=event.id, user_id=event.user_id):
            # the same per-user lock handle_event takes: a chat turn and a background event of one user
            # (different lanes) still never touch their state at once
            async with runner._event_lock(event):
                await run_with_inline_retries(attempt, what="event", ref=event.id)

    async def _heartbeat(self, uid: int, lane: Lane, owner: str) -> None:
        while True:
            await asyncio.sleep(30)
            await self._box.renew(uid, lane, owner)

    async def run_executor(self, lane: Lane, name: str) -> None:
        owner = f"{name}:{uuid.uuid4().hex[:8]}"
        while True:
            if not await self._run_one(lane, owner):
                if self._reap_on_idle:
                    await self._box.reap()
                self._wake[lane].clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake[lane].wait(), timeout=1.0)

    async def drain(self) -> None:
        """Tests and `mavis chat`: run until every mailbox is empty."""
        while True:
            busy = await asyncio.gather(*(self._run_one(ln, f"drain-{ln}-{i}")
                                          for ln in ("chat", "bg") for i in range(self._n[ln])))
            if not any(busy):
                return

    def executors(self, name: str) -> list:
        return [self.run_executor(ln, f"{name}-{ln}-{i}")
                for ln in ("chat", "bg") for i in range(self._n[ln])]
