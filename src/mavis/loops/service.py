"""Open loops: the PA's mental list of unfinished business.

Every create/update is published as a LOOP_* event so the initiative engine
can plan its own wakeups around it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.exc import NoResultFound

from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Provenance, Trust
from mavis.domain.loops import Loop, LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.memory import Extraction
from mavis.domain.messages import Role
from mavis.store.repo import loops as repo
from mavis.store.repo import messages, users
from mavis.worker.locks import lock

log = structlog.get_logger()
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)
MIN_EVENT_IMPORTANCE = 3
TITLE_MAX = 300
REOPEN_GUARD = timedelta(days=7)
FOLLOW_UP_MATCH = timedelta(minutes=2)  # follow-up logged, then the loop marked AWAITING right after


def _sort(loops: list[Loop]) -> list[Loop]:
    return sorted(loops, key=lambda lp: (lp.due_at is None, lp.due_at or _FAR_FUTURE, -lp.importance, lp.id))


class LoopService:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def upsert(self, user_id: int, data: LoopUpsert) -> Loop:
        # Distinct key from `user:{id}`: turn hooks already hold that one, so this cannot self-deadlock.
        async with lock(f"loops:{user_id}"):
            changed = False
            if data.id is not None:
                result = await repo.update(user_id, data.id, data)
                if result is None:
                    raise ValueError(f"loop {data.id} does not exist")
                loop, changed = result
                created = False
            elif (existing := await repo.find_open_duplicate(user_id, data)) is not None:
                # merge: keep the more specific title (more identifying words), else the established one
                specific = len(repo.title_tokens(data.title)) > len(repo.title_tokens(existing.title))
                title = data.title if specific else existing.title
                merged = data.model_copy(update={"title": title,
                                                 "importance": max(existing.importance, data.importance)})
                result = await repo.update(user_id, existing.id, merged)
                assert result is not None
                loop, changed = result
                created = False
            else:
                loop = await repo.insert(user_id, data)
                created = True
            # Re-publish LOOP_CREATED on every path: the bus dedupes by id, so a retry after a crash
            # between commit and publish still gets the event out.
            await self._emit(loop, EventType.LOOP_CREATED)
            if changed and not created:
                await self._emit(loop, EventType.LOOP_UPDATED)
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
        current = await repo.get(loop_id)
        if current is None:
            return None
        async with lock(f"loops:{current.user_id}"):
            result = await repo.set_status(current.user_id, loop_id, status)
            if result is None:
                return None
            loop, changed = result
            if changed:
                await self._emit(loop, EventType.LOOP_UPDATED)
        return loop

    async def on_user_message(self, user_id: int, text: str) -> int:
        """Close loops whose follow-up the user is answering: the message names the loop, or it directly
        follows that loop's follow-up message (not just any proactive message)."""
        now = timeutil.now()
        awaiting = await repo.list_awaiting(user_id, now - repo.AWAITING_FOR)
        if not awaiting:
            return 0
        said = set(repo.title_tokens(text))
        replying_since = await _proactive_reply_anchor(user_id)
        closed = 0
        for loop, waiting_since in awaiting:
            named = said & set(repo.title_tokens(" ".join([loop.title, *loop.entities])))
            # the message right before this one is this loop's follow-up: AWAITING is set the moment the
            # follow-up is delivered, so that proactive message was logged just before waiting_since
            direct = replying_since is not None and \
                timedelta(0) <= waiting_since - replying_since <= FOLLOW_UP_MATCH
            if named or direct:
                await self.close(loop.id, LoopStatus.DONE)
                closed += 1
        if closed:
            log.info("loops.closed_on_reply", user_id=user_id, count=closed)
        return closed

    async def expire_stale(self) -> int:
        total = 0
        for user_id in await repo.open_user_ids():
            async with lock(f"loops:{user_id}"):
                expired = await repo.expire(user_id, timeutil.now())
                for loop in expired:
                    await self._emit(loop, EventType.LOOP_UPDATED)
            total += len(expired)
        return total

    async def _emit(self, loop: Loop, event_type: EventType) -> None:
        created = event_type is EventType.LOOP_CREATED
        event = Event(
            id=f"loop:{loop.id}:created" if created else f"loop:{loop.id}:updated:{loop.version}",
            user_id=loop.user_id,
            type=event_type,
            occurred_at=timeutil.now(),
            source="agent",
            payload=loop.model_dump(mode="json"),
            # the event carries the loop's provenance: a loop derived from third-party content is
            # reasoned about as untrusted (wrapped, no new work, capped urgency, untrusted wakeups)
            trust=Trust.SYSTEM if loop.trusted else Trust.UNTRUSTED,
        )
        await self._bus.publish(event)


async def _proactive_reply_anchor(user_id: int) -> datetime | None:
    """When the user's latest message directly follows a proactive message: that message's time."""
    recent = await messages.recent(user_id, 3)
    if len(recent) < 2 or recent[-1].role != Role.USER.value:
        return None
    prev = recent[-2]
    if prev.role != Role.ASSISTANT.value or not prev.proactive:
        return None
    return timeutil.ensure_utc(prev.created_at)


async def _upsert_unless_closed(service: LoopService, user_id: int, data: LoopUpsert) -> None:
    """Skip a loop the user already closed in the last week: re-extraction must not resurrect it."""
    data.title = data.title.strip()[:TITLE_MAX]
    if not data.title:
        return
    if await repo.find_recently_closed(user_id, data.title, timeutil.now() - REOPEN_GUARD):
        log.info("loops.reopen_skipped", user_id=user_id)
        return
    await service.upsert(user_id, data)


def extraction_trust(prov: Provenance) -> Trust:
    """Only the user's own words, in a turn that saw no third-party content, are trusted."""
    return Trust.USER if prov.trust is Trust.USER else Trust.UNTRUSTED


async def loops_from_extraction(
    service: LoopService, user_id: int, extraction: Extraction, prov: Provenance
) -> None:
    """MemoryService.on_extraction hook: turn extracted loops/events into open loops.

    Loops come only from conversation turns (spec 8.3); an ingested document (first sync, signals)
    never creates one. A turn that saw untrusted content (a tainted reply, tool output) yields
    untrusted loops, and that trust follows them everywhere (events, wakeups, briefs, recall)."""
    if not prov.conversation:
        log.info("loops.non_conversation_skipped", source_ref=prov.source_ref[:40])
        return
    trust = extraction_trust(prov)
    source_ref = prov.source_ref
    try:
        user = await users.get(user_id)
    except NoResultFound:
        log.warning("loops.user_missing", user_id=user_id)
        return
    for draft in extraction.loops:
        try:
            kind = LoopKind(draft.kind.strip().upper())
        except ValueError:
            kind = LoopKind.COMMITMENT
        due = timeutil.to_utc(draft.due_at, user.timezone) if draft.due_at else None
        await _upsert_unless_closed(
            service,
            user_id,
            LoopUpsert(
                kind=kind,
                title=draft.title,
                due_at=due,
                entities=draft.entities,
                importance=draft.importance,
                source=source_ref,
                trust=trust,
                origin=LoopOrigin.CONVERSATION,
            ),
        )
    for ev in extraction.events:
        if ev.ambiguous or ev.starts_at is None or ev.importance < MIN_EVENT_IMPORTANCE:
            continue
        await _upsert_unless_closed(
            service,
            user_id,
            LoopUpsert(
                kind=LoopKind.COMMITMENT,
                title=ev.title,
                due_at=timeutil.to_utc(ev.starts_at, user.timezone),
                entities=ev.with_people,
                importance=ev.importance,
                source=source_ref,
                trust=trust,
                origin=LoopOrigin.CONVERSATION,
            ),
        )
