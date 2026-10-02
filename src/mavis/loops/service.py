"""Open loops: the PA's mental list of unfinished business.

Every create/update is published as a LOOP_* event so the initiative engine
can plan its own wakeups around it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog

from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import Loop, LoopKind, LoopStatus, LoopUpsert
from mavis.domain.memory import Extraction
from mavis.store.repo import loops as repo
from mavis.store.repo import users
from mavis.worker.locks import lock

log = structlog.get_logger()
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)
MIN_EVENT_IMPORTANCE = 3
# Loops are created only from conversation turns the user typed (spec 8.3). LEARN's source_ref is the
# originating event id: "tg:update:N" (Telegram) or "cli:<uuid>" (`mavis chat`). Anything else
# (email, web, task output) is untrusted data and never creates loops here.
TRUSTED_SOURCE_PREFIXES = ("tg:", "chat:", "local:", "cli:")


def _sort(loops: list[Loop]) -> list[Loop]:
    return sorted(loops, key=lambda lp: (lp.due_at is None, lp.due_at or _FAR_FUTURE, -lp.importance, lp.id))


class LoopService:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def upsert(self, user_id: int, data: LoopUpsert) -> Loop:
        # Distinct key from `user:{id}`: turn hooks already hold that one, so this cannot self-deadlock.
        async with lock(f"loops:{user_id}"):
            if data.id is not None:
                loop = await repo.update(data.id, data)
                if loop is None:
                    raise ValueError(f"loop {data.id} does not exist")
                created = False
            elif (existing := await repo.find_open_duplicate(user_id, data)) is not None:
                loop = await repo.update(existing.id, data)
                assert loop is not None
                created = False
            else:
                loop = await repo.insert(user_id, data)
                created = True
        await self._emit(loop, created)
        return loop

    async def active(
        self, user_id: int, entities: list[str] | None = None, due_within: timedelta | None = None
    ) -> list[Loop]:
        loops = await repo.list_open(user_id)
        if entities is None and due_within is None:
            return _sort(loops)
        names = {e.casefold() for e in entities or []}
        horizon = timeutil.now() + due_within if due_within else None
        keep = [
            lp
            for lp in loops
            if (names and names & {e.casefold() for e in lp.entities})
            or (horizon is not None and lp.due_at is not None and lp.due_at <= horizon)
        ]
        return _sort(keep)

    async def get(self, loop_id: int) -> Loop | None:
        return await repo.get(loop_id)

    async def close(self, loop_id: int, status: LoopStatus = LoopStatus.DONE) -> Loop | None:
        loop = await repo.set_status(loop_id, status)
        if loop is not None:
            await self._emit(loop, created=False)
        return loop

    async def expire_stale(self) -> int:
        expired = await repo.expire(timeutil.now())
        for loop in expired:
            await self._emit(loop, created=False)
        return len(expired)

    async def _emit(self, loop: Loop, created: bool) -> None:
        event = Event(
            id=f"loop:{loop.id}:created" if created else f"loop:{loop.id}:updated:{uuid4().hex[:12]}",
            user_id=loop.user_id,
            type=EventType.LOOP_CREATED if created else EventType.LOOP_UPDATED,
            occurred_at=timeutil.now(),
            source="agent",
            payload=loop.model_dump(mode="json"),
            trust=Trust.SYSTEM,
        )
        await self._bus.publish(event)


async def loops_from_extraction(
    service: LoopService, user_id: int, extraction: Extraction, source_ref: str
) -> None:
    """MemoryService.on_extraction hook: turn extracted loops/events into open loops."""
    if not source_ref.startswith(TRUSTED_SOURCE_PREFIXES):
        log.info("loops.untrusted_source_skipped", source_ref=source_ref[:40])
        return
    user = await users.get(user_id)
    for draft in extraction.loops:
        try:
            kind = LoopKind(draft.kind.strip().upper())
        except ValueError:
            kind = LoopKind.COMMITMENT
        due = timeutil.to_utc(draft.due_at, user.timezone) if draft.due_at else None
        await service.upsert(
            user_id,
            LoopUpsert(
                kind=kind,
                title=draft.title,
                due_at=due,
                entities=draft.entities,
                importance=draft.importance,
                source=source_ref,
            ),
        )
    for ev in extraction.events:
        if ev.ambiguous or ev.starts_at is None or ev.importance < MIN_EVENT_IMPORTANCE:
            continue
        await service.upsert(
            user_id,
            LoopUpsert(
                kind=LoopKind.COMMITMENT,
                title=ev.title,
                due_at=timeutil.to_utc(ev.starts_at, user.timezone),
                entities=ev.with_people,
                importance=ev.importance,
                source=source_ref,
            ),
        )
