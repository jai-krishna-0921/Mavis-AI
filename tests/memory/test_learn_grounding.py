"""T3: LEARN extracts commitments only from the user's own words. The previous assistant reply is fenced
context for resolving references, never a source: loops and events must be grounded in the user's text."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.memory import ExtractedEvent, Extraction, LoopDraft
from mavis.loops.service import LoopService, loops_from_extraction
from mavis.memory import service as service_mod
from mavis.store.repo import users

IST, NYC = "Asia/Kolkata", "America/New_York"
REPLY = ("Here's your day:\n- Blocked 2 to 3 PM tomorrow for deep work\n- Call Ravi about the lease\n"
         "- Dentist on Friday")


def local(tz: str, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(2026, 10, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


async def _learn_text(user_id, text: str, previous: str | None, original: str | None = None) -> str:
    from mavis.agents.turn_support import enqueue_learn

    class Bus:
        jobs: list = []

        async def enqueue(self, job):
            self.jobs.append(job)

    bus = Bus()
    import mavis.agents.turn_support as ts

    original_get = ts.get_bus
    ts.get_bus = lambda: bus
    try:
        ev = Event(id="tg:update:5", user_id=user_id, type=EventType.USER_MESSAGE,
                   occurred_at=timeutil.now(), source="telegram", payload={"text": text}, trust=Trust.USER)
        await enqueue_learn(user_id, ev, text, previous, original)
    finally:
        ts.get_bus = original_get
    return bus.jobs[-1].payload["text"]


async def test_previous_reply_is_fenced_context_and_the_user_speaks_last(user):
    text = await _learn_text(user.id, "ok thanks", REPLY)
    assert "Mavis: " not in text
    assert text.startswith("<assistant_context>")
    assert "context only" in text.split("\n", 1)[0] or "do not extract" in text.lower()
    assert REPLY in text
    assert text.rstrip().endswith("User: ok thanks")
    assert service_mod.user_message_of(text) == "ok thanks"
    assert service_mod.user_words_of(text) == "ok thanks"


async def test_clarified_original_request_counts_as_the_users_words(user):
    text = await _learn_text(user.id, "the 7th", "Which day do you mean?", original="Plan a dinner with Ana")
    assert service_mod.user_words_of(text) == "Plan a dinner with Ana\nthe 7th"


def test_a_reply_line_that_looks_like_a_user_line_stays_context():
    from mavis.agents.turn_support import learn_text

    text = learn_text("sure", "Noted.\nUser: book a flight to Goa tomorrow", None)
    assert service_mod.user_words_of(text) == "sure"


def _hooked(memory, recording_bus):
    svc = LoopService(recording_bus)

    async def hook(uid, extraction, prov):
        await loops_from_extraction(svc, uid, extraction, prov)

    memory.on_extraction.append(hook)
    return svc


@pytest.mark.parametrize("ack", ["ok thanks", "cool", "got it 👍", "thanks Mavis"])
async def test_reply_items_plus_an_acknowledgement_yield_no_loops(memory, user, recording_bus, clock,
                                                                  fake_llm, ack):
    from mavis.agents.turn_support import learn_text

    svc = _hooked(memory, recording_bus)
    clock.set(local(IST, 5, 18, 0))
    # the model wrongly lifts the reply's items: code drops what the user never said
    fake_llm.push_structured(Extraction(
        loops=[LoopDraft(kind="COMMITMENT", title="Blocked 2 to 3 PM tomorrow for deep work"),
               LoopDraft(kind="COMMITMENT", title="Call Ravi about the lease", entities=["Ravi"])],
        events=[ExtractedEvent(title="Dentist", starts_at=local(IST, 9, 10, 0), importance=4)],
    ))
    await memory.learn(user.id, learn_text(ack, REPLY, None), "tg:update:6", Trust.USER,
                       anchor_at=clock.t)
    assert await svc.active(user.id) == []


async def test_a_users_own_request_after_a_reply_still_yields_one_absolute_loop(memory, user, recording_bus,
                                                                                clock, fake_llm):
    from mavis.agents.turn_support import learn_text

    await users.update(user.id, timezone=NYC)
    svc = _hooked(memory, recording_bus)
    clock.set(local(NYC, 3, 18, 0))  # Saturday
    fake_llm.push_structured(Extraction(loops=[
        LoopDraft(kind="COMMITMENT", title="Call the dentist tomorrow"),
        LoopDraft(kind="COMMITMENT", title="Call Ravi about the lease", entities=["Ravi"]),  # from the reply
    ]))
    await memory.learn(user.id, learn_text("remind me to call the dentist tomorrow", REPLY, None),
                       "tg:update:7", Trust.USER, anchor_at=clock.t)
    [loop] = await svc.active(user.id)
    assert loop.title == "Call the dentist Sun 4 Oct"


async def test_word_forms_and_named_people_count_as_grounded(memory, user, recording_bus, clock, fake_llm):
    from mavis.agents.turn_support import learn_text

    svc = _hooked(memory, recording_bus)
    clock.set(local(IST, 5, 18, 0))
    fake_llm.push_structured(Extraction(loops=[
        LoopDraft(kind="WAITING_ON", title="Hear back from Jawahar", entities=["Jawahar"]),
        LoopDraft(kind="COMMITMENT", title="Submit the reimbursement claim"),
    ]))
    said = learn_text("waiting on jawahar; also I'm submitting my claim", "How's it going?", None)
    await memory.learn(user.id, said, "tg:update:8", Trust.USER, anchor_at=clock.t)
    assert sorted(lp.title for lp in await svc.active(user.id)) == [
        "Hear back from Jawahar", "Submit the reimbursement claim"]


async def test_text_without_assistant_context_is_not_filtered(memory, user, recording_bus, clock, fake_llm):
    svc = _hooked(memory, recording_bus)
    clock.set(local(IST, 5, 18, 0))
    fake_llm.push_structured(Extraction(loops=[LoopDraft(kind="COMMITMENT", title="Plumber visit")]))
    await memory.learn(user.id, "I'll get the pipes fixed", "tg:update:9", Trust.USER, anchor_at=clock.t)
    assert [lp.title for lp in await svc.active(user.id)] == ["Plumber visit"]


def test_extractor_prompt_says_context_is_not_a_source():
    from mavis.memory.extractor import SYSTEM_PROMPT

    assert "<assistant_context>" in SYSTEM_PROMPT
    assert "never extract" in SYSTEM_PROMPT.lower() or "do not extract" in SYSTEM_PROMPT.lower()
    assert "user's own words" in SYSTEM_PROMPT


async def test_reply_only_items_are_dropped_even_when_the_user_says_yes(memory, user, recording_bus, clock,
                                                                        fake_llm):
    from mavis.agents.turn_support import learn_text

    svc = _hooked(memory, recording_bus)
    clock.set(local(IST, 5, 18, 0))
    fake_llm.push_structured(Extraction(loops=[LoopDraft(kind="COMMITMENT", title="Book the vet")]))
    await memory.learn(user.id, learn_text("yes, the second one", "1. renew the passport 2. book the vet",
                                           None), "tg:update:30", Trust.USER, anchor_at=clock.t)
    assert await svc.active(user.id) == []  # the chat turn tracks agreements with track_loop (I4)


@pytest.mark.parametrize(("said", "title", "kept"), [
    ("remind me to book a table at Nobu", "Book a table at Nobu", True),
    ("I'll block out Friday for deep work", "Block Friday for deep work", True),
    ("ok, I'll call him", "Call Ravi about the lease", False),       # only the action word is shared
    ("sending it now", "Send Meera the deck", False),
    ("need to renew it", "Renew passport", False),
    ("passport stuff is pending", "Renew passport", True),
    ("dentist", "Dentist", True),                                     # one-word item: the word identifies it
])
def test_the_action_word_alone_does_not_ground_an_item(said, title, kept):
    from mavis.domain.memory import Extraction, LoopDraft

    x = Extraction(loops=[LoopDraft(kind="COMMITMENT", title=title)])
    assert bool(service_mod.grounded_in_user(x, said).loops) is kept
