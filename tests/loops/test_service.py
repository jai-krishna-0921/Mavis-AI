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


async def test_learn_retry_yields_one_loop_and_one_created_event(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    for _ in range(2):
        await loops_from_extraction(svc, user.id, _one_loop_extraction(), "tg:update:5")
    assert len(await svc.active(user.id)) == 1
    ids = [e.id for e in recording_bus.events]
    assert [i for i in ids if i.endswith(":created")] == [ids[0]]
    assert len(ids) == 1


async def test_unchanged_update_emits_no_updated_event(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    data = LoopUpsert(kind=LoopKind.COMMITMENT, title="Same", due_at=DUE)
    first = await svc.upsert(user.id, data)
    await svc.upsert(user.id, data)
    await svc.upsert(user.id, data)
    assert [e.type for e in recording_bus.events] == [EventType.LOOP_CREATED]
    await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Same", due_at=DUE, entities=["A"]))
    assert recording_bus.events[-1].id == f"loop:{first.id}:updated:2"


async def test_close_emits_deterministic_id_and_is_idempotent(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    loop = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.GOAL, title="g"))
    recording_bus.take()
    await svc.close(loop.id)
    await svc.close(loop.id)
    assert [e.id for e in recording_bus.take()] == [f"loop:{loop.id}:updated:2"]


async def test_hook_does_not_resurrect_recently_closed_loop(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    await loops_from_extraction(svc, user.id, _one_loop_extraction(), "tg:update:1")
    [loop] = await svc.active(user.id)
    await svc.close(loop.id)
    again = Extraction(loops=[LoopDraft(kind="commitment", title="  send the DECK! ")])
    await loops_from_extraction(svc, user.id, again, "tg:update:2")
    assert await svc.active(user.id) == []
    clock.advance(days=8)
    await loops_from_extraction(svc, user.id, again, "tg:update:3")
    assert len(await svc.active(user.id)) == 1


async def test_hook_truncates_long_title_and_skips_missing_user(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    long = Extraction(loops=[LoopDraft(kind="goal", title="x" * 500)])
    await loops_from_extraction(svc, user.id, long, "tg:update:1")
    assert len((await svc.active(user.id))[0].title) == 300
    await loops_from_extraction(svc, 9999, long, "tg:update:2")


async def test_upsert_cannot_touch_another_users_loop(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    loop = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.GOAL, title="g"))
    with pytest.raises(ValueError):
        await svc.upsert(user.id + 1, LoopUpsert(id=loop.id, kind=LoopKind.GOAL, title="hijack"))


async def test_fuzzy_duplicate_from_one_utterance_is_merged(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    a = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dentist appointment",
                                             due_at=DUE, importance=3))
    b = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dentist appointment at 4pm",
                                             due_at=DUE + timedelta(minutes=30), importance=4))
    c = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Appointment with the dentist "
                                             "tomorrow", importance=2))
    assert a.id == b.id == c.id
    [loop] = await svc.active(user.id)
    assert loop.title == "Dentist appointment" and loop.importance == 4
    assert loop.due_at == DUE + timedelta(minutes=30)


@pytest.mark.parametrize("title, due_shift, kind", [
    ("Dentist appointment", timedelta(hours=3), LoopKind.COMMITMENT),  # far apart: a second visit
    ("Call the dentist", timedelta(0), LoopKind.COMMITMENT),            # a different thing
    ("Dentist appointment", timedelta(0), LoopKind.CONCERN),            # different kind
])
async def test_distinct_loops_are_not_merged(user, recording_bus, clock, title, due_shift, kind):
    svc = LoopService(recording_bus)
    await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dentist appointment", due_at=DUE))
    await svc.upsert(user.id, LoopUpsert(kind=kind, title=title, due_at=DUE + due_shift))
    assert len(await svc.active(user.id)) == 2


def test_similar_titles_ignores_times_dates_and_filler():
    from mavis.store.repo.loops import similar_titles

    assert similar_titles("Interview with Jawahar at Fractal", "Fractal interview, Jawahar, Monday 10am")
    assert similar_titles("Send thank-you email to Jawahar", "send thank you email to jawahar")
    assert not similar_titles("Interview with Jawahar", "Thank-you email to Jawahar")


@pytest.mark.parametrize("a, b", [
    ("call mom", "call tom"),
    ("email Raj", "email Ram"),
    ("flight to Delhi", "flight to Dubai"),
    ("invoice to Acme", "invoice to Apex"),
    ("pay rent", "pay rest"),
    ("Reply to John", "Reply to Joan"),
    ("call mom", "call bank"),
    ("Email Raj", "Email Raj and Priya"),  # the longer title adds a person
])
def test_similar_titles_keeps_short_or_name_differences_apart(a, b):
    from mavis.store.repo.loops import similar_titles

    assert not similar_titles(a, b)


def test_similar_titles_merges_rephrasings_and_long_typos():
    from mavis.store.repo.loops import similar_titles

    assert similar_titles("Dentist appointment", "Dentist appointment at 4pm")
    assert similar_titles("Dentist appointment", "dentist appointmnet")  # long non-name word typo
    assert similar_titles("Prepare slides", "Prepare quarterly review slides")  # subset of 2+ words
    assert not similar_titles("Interview with Jawahar", "Interview with Jawahr")  # names never fuzzed


async def test_dentist_phrasings_still_merge_in_service(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    a = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dentist appointment",
                                             due_at=DUE))
    b = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dentist appointment at 4pm",
                                             due_at=DUE))
    assert a.id == b.id


async def test_update_by_id_does_not_reopen_an_awaiting_loop(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    loop = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview", due_at=DUE))
    await svc.close(loop.id, LoopStatus.AWAITING_REPLY)
    updated = await svc.upsert(user.id, LoopUpsert(id=loop.id, kind=LoopKind.COMMITMENT, title="Interview",
                                                   entities=["Jawahar"]))
    assert updated.status is LoopStatus.AWAITING_REPLY and updated.entities == ["Jawahar"]
    done = await svc.upsert(user.id, LoopUpsert(id=loop.id, kind=LoopKind.COMMITMENT, title="Interview",
                                                status=LoopStatus.DONE))
    assert done.status is LoopStatus.DONE  # an explicit close still applies


async def test_subset_merge_keeps_the_more_specific_title(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    a = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Renew passport"))
    b = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Renew passport at the office"))
    assert a.id == b.id and b.title == "Renew passport at the office"
    c = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Email Raj"))
    d = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Email Raj and Priya"))
    assert c.id != d.id
