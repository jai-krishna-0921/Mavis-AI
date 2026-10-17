from datetime import timedelta

from mavis.domain import timeutil
from mavis.domain.events import EventType
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.messages import Role
from mavis.loops.service import LoopService
from mavis.store.repo import loops as repo
from mavis.store.repo import messages


async def test_list_changed_since_includes_closed_and_filters_kind(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    since = timeutil.now() - timedelta(minutes=1)
    a = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Send the deck"))
    b = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.GOAL, title="Run a marathon"))
    await svc.upsert(user.id, LoopUpsert(kind=LoopKind.WATCH, title="Watch the invoice thread"))
    await svc.close(a.id, LoopStatus.DONE)
    got = await repo.list_changed_since(user.id, since, frozenset({LoopKind.COMMITMENT, LoopKind.GOAL}))
    assert {lp.id for lp in got} == {a.id, b.id}
    assert await repo.list_changed_since(user.id, timeutil.now() + timedelta(days=1),
                                         frozenset({LoopKind.COMMITMENT})) == []
    closed = await repo.list_closed_before(user.id, timeutil.now() + timedelta(days=1),
                                           frozenset({LoopKind.COMMITMENT, LoopKind.GOAL}))
    assert [lp.id for lp in closed] == [a.id]
    assert await repo.list_closed_before(user.id, since, frozenset({LoopKind.COMMITMENT})) == []


async def test_create_exact_does_not_merge_similar_title(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    a = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview prep"))
    b = await svc.create_exact(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="interview PREP",
                                                   origin=LoopOrigin.NOTION))
    assert a.id != b.id and b.origin is LoopOrigin.NOTION
    assert [e.type for e in recording_bus.take()] == [EventType.LOOP_CREATED, EventType.LOOP_CREATED]
    assert len(await svc.active(user.id)) == 2


async def test_proactive_since_prefix(user):
    t0 = timeutil.now() - timedelta(minutes=5)
    await messages.log(user.id, Role.ASSISTANT, "morning", proactive=True, event_id="proactive:morning:d1")
    await messages.log(user.id, Role.ASSISTANT, "evening", proactive=True, event_id="proactive:morning:d2")
    await messages.log(user.id, Role.ASSISTANT, "other", proactive=True, event_id="proactive:evening:d1")
    await messages.log(user.id, Role.USER, "hi", event_id="proactive:morning:d3")
    got = await messages.proactive_since(user.id, "proactive:morning:", t0)
    assert [m.content for m in got] == ["evening", "morning"]
    later = timeutil.now() + timedelta(days=1)
    assert await messages.proactive_since(user.id, "proactive:morning:", later) == []
