from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain import timeutil
from mavis.domain.events import EventType
from mavis.domain.loops import LoopKind, LoopStatus, LoopUpsert, WatchSpec
from mavis.domain.memory import ExtractedEvent, Extraction, LoopDraft
from mavis.loops.service import LoopService, loops_from_extraction

DUE = datetime(2026, 9, 28, 4, 30, tzinfo=UTC)  # Mon 10:00 IST


async def test_upsert_creates_and_emits(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    loop = await svc.upsert(
        user.id,
        LoopUpsert(
            kind=LoopKind.COMMITMENT, title="Interview prep", due_at=DUE, entities=["Jawahar"], importance=5
        ),
    )
    assert loop.id > 0 and loop.status is LoopStatus.OPEN and loop.due_at == DUE
    [event] = recording_bus.take()
    assert event.type is EventType.LOOP_CREATED
    assert event.payload["id"] == loop.id and event.payload["title"] == "Interview prep"


async def test_same_open_loop_is_updated_not_duplicated(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    a = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview prep", due_at=DUE))
    b = await svc.upsert(
        user.id,
        LoopUpsert(kind=LoopKind.COMMITMENT, title="interview PREP", due_at=DUE, entities=["Jawahar"]),
    )
    assert a.id == b.id
    assert b.entities == ["Jawahar"]
    assert [e.type for e in recording_bus.take()] == [EventType.LOOP_CREATED, EventType.LOOP_UPDATED]
    assert len(await svc.active(user.id)) == 1


async def test_active_filters_by_entity_or_due_window(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    now = timeutil.now()
    a = await svc.upsert(
        user.id,
        LoopUpsert(kind=LoopKind.COMMITMENT, title="A", entities=["Jawahar"], due_at=now + timedelta(days=5)),
    )
    b = await svc.upsert(
        user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="B", due_at=now + timedelta(days=1))
    )
    c = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.GOAL, title="C"))
    picked = await svc.active(user.id, entities=["jawahar"], due_within=timedelta(hours=48))
    assert {loop.id for loop in picked} == {a.id, b.id}
    assert [loop.id for loop in await svc.active(user.id)] == [b.id, a.id, c.id]


async def test_close_marks_done_hides_and_emits(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    loop = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Reply from recruiter"))
    recording_bus.take()
    closed = await svc.close(loop.id)
    assert closed is not None and closed.status is LoopStatus.DONE
    assert await svc.active(user.id) == []
    [event] = recording_bus.take()
    assert event.type is EventType.LOOP_UPDATED and event.payload["status"] == "DONE"


async def test_expire_stale(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    now = timeutil.now()
    await svc.upsert(
        user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="old", due_at=now - timedelta(days=3))
    )
    await svc.upsert(
        user.id,
        LoopUpsert(
            kind=LoopKind.WATCH,
            title="watch",
            watch=WatchSpec(keywords=["x"], deadline=now - timedelta(hours=1)),
        ),
    )
    await svc.upsert(user.id, LoopUpsert(kind=LoopKind.GOAL, title="goal", due_at=now - timedelta(days=30)))
    await svc.upsert(
        user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="fresh", due_at=now - timedelta(hours=5))
    )
    assert await svc.expire_stale() == 2
    assert {loop.title for loop in await svc.active(user.id)} == {"goal", "fresh"}


async def test_extraction_hook_creates_loops(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    extraction = Extraction(
        events=[
            ExtractedEvent(title="Coffee", starts_at=None, ambiguous=True),
            ExtractedEvent(title="Haircut", starts_at=datetime(2026, 9, 29, 9, 0), importance=2),
            ExtractedEvent(
                title="Interview prep with Jawahar",
                starts_at=datetime(2026, 9, 28, 10, 0),
                with_people=["Jawahar"],
                importance=5,
            ),
        ],
        loops=[LoopDraft(kind="waiting_on", title="Referral from Jawahar", entities=["Jawahar"])],
    )
    await loops_from_extraction(svc, user.id, extraction, "tg:update:7")
    loops = {loop.title: loop for loop in await svc.active(user.id)}
    assert set(loops) == {"Interview prep with Jawahar", "Referral from Jawahar"}
    assert loops["Interview prep with Jawahar"].due_at == DUE  # naive 10:00 interpreted as IST
    assert loops["Interview prep with Jawahar"].kind is LoopKind.COMMITMENT
    assert loops["Referral from Jawahar"].kind is LoopKind.WAITING_ON
    assert loops["Referral from Jawahar"].source == "tg:update:7"


def _one_loop_extraction() -> Extraction:
    return Extraction(loops=[LoopDraft(kind="commitment", title="Send the deck")])


@pytest.mark.parametrize("source_ref", ["tg:update:9", "cli:7c1e", "chat:abc", "local:1"])
async def test_extraction_hook_trusts_conversation_sources(user, recording_bus, clock, source_ref):
    svc = LoopService(recording_bus)
    await loops_from_extraction(svc, user.id, _one_loop_extraction(), source_ref)
    assert [loop.title for loop in await svc.active(user.id)] == ["Send the deck"]


@pytest.mark.parametrize("source_ref", ["gmail:msg-1", "web:https://x.test", "task:5", ""])
async def test_extraction_hook_ignores_untrusted_sources(user, recording_bus, clock, source_ref):
    svc = LoopService(recording_bus)
    await loops_from_extraction(svc, user.id, _one_loop_extraction(), source_ref)
    assert await svc.active(user.id) == []
    assert recording_bus.take() == []


async def test_real_conversation_turn_creates_loop(user, memory, recording_bus, clock, fake_llm):
    """The source_ref a real turn hands to LEARN (the event id) passes the trust gate."""
    from mavis.domain.events import Trust

    svc = LoopService(recording_bus)

    async def hook(uid, extraction, source_ref):
        await loops_from_extraction(svc, uid, extraction, source_ref)

    memory.on_extraction.append(hook)
    fake_llm.push_structured(_one_loop_extraction())
    await memory.learn(user.id, "User: I'll send the deck", source_ref="tg:update:42", trust=Trust.USER)
    assert [lp.source for lp in await svc.active(user.id)] == ["tg:update:42"]
