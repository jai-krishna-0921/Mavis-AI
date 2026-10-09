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


# --- I5: facts, entities, relations and profile updates are grounded the same way -------------------


def _x(**kw):
    from mavis.domain.memory import Entity, Extraction, ProfileUpdate, Relation  # noqa: F401

    return Extraction(**kw)


def test_reply_derived_entities_relations_and_profile_updates_are_dropped():
    from mavis.domain.memory import Entity, ProfileUpdate, Relation

    x = _x(
        entities=[Entity(name="Ravi Menon", label="Person"), Entity(name="Siemens", label="Organization"),
                  Entity(name="User", label="User")],
        relations=[
            Relation(subject="Ravi Menon", rel="WORKS_AT", object="Siemens",
                     statement="Ravi works at Siemens."),
            Relation(subject="User", rel="FRIEND_OF", object="Ravi Menon", statement="Ravi is a friend."),
            Relation(subject="User", rel="PREFERS", object="Morning meetings",
                     statement="Prefers morning meetings."),
        ],
        profile_updates=[ProfileUpdate(field="goals", value="Run a marathon"),
                         ProfileUpdate(field="tone", value="casual")],
    )
    out = service_mod.grounded_in_user(x, "ravi is my friend, keep it casual")
    assert [e.name for e in out.entities] == ["Ravi Menon", "User"]
    assert [r.statement for r in out.relations] == ["Ravi is a friend."]
    assert [p.value for p in out.profile_updates] == ["casual"]


async def test_learn_keeps_only_user_grounded_facts(memory, user, fake_llm, clock):
    from mavis.agents.turn_support import learn_text
    from mavis.domain.memory import Entity, Relation

    clock.set(local(IST, 5, 18, 0))
    fake_llm.push_structured(_x(
        entities=[Entity(name="Jawahar", label="Person"), Entity(name="Acme", label="Organization")],
        relations=[
            Relation(subject="Jawahar", rel="WORKS_AT", object="Acme", statement="Jawahar works at Acme."),
            Relation(subject="User", rel="FRIEND_OF", object="Jawahar", statement="Jawahar is Jai's friend."),
        ],
    ))
    reply = "Jawahar works at Acme now, by the way."
    await memory.learn(user.id, learn_text("cool, jawahar is a good friend", reply, None), "tg:update:40",
                       Trust.USER, anchor_at=clock.t)
    statements = [d["statement"] for d in await memory.graph.dump(user.id)]
    assert statements == ["Jawahar is Jai's friend."]
    assert {e.name for e in await memory.graph.entities(user.id)} >= {"Jawahar"}
    assert "Acme" not in {e.name for e in await memory.graph.entities(user.id)}


# --- a noun-first title is grounded by its first word when that word is the user's, not an echo -------


@pytest.mark.parametrize(("said", "title"), [
    ("dentist on friday", "Dentist appointment Friday"),
    ("gym at 6 tomorrow", "Gym session at 6 tomorrow"),
    ("passport by the 20th", "Passport renewal by the 20th"),
])
async def test_the_users_own_first_word_grounds_a_noun_first_item(memory, user, recording_bus, clock,
                                                                  fake_llm, said, title):
    from mavis.agents.turn_support import learn_text

    svc = _hooked(memory, recording_bus)
    clock.set(local(IST, 5, 18, 0))
    fake_llm.push_structured(Extraction(loops=[LoopDraft(kind="COMMITMENT", title=title)]))
    await memory.learn(user.id, learn_text(said, "Anything else on your mind?", None), "tg:update:50",
                       Trust.USER, anchor_at=clock.t)
    assert len(await svc.active(user.id)) == 1


@pytest.mark.parametrize(("ack", "reply", "title"), [
    ("ok", "Want me to add a dentist appointment on Friday?", "Dentist appointment Friday"),
    ("sure", "Shall I put a gym session at 6 tomorrow on your list?", "Gym session at 6 tomorrow"),
    ("yeah do that", "Passport renewal by the 20th?", "Passport renewal by the 20th"),
    ("yeah book it", "Want me to book the gym session at 6?", "Book gym session at 6"),
    ("ok, I'll call him", "Call Ravi about the lease?", "Call Ravi about the lease"),
    ("sending it now", "Remember to send Meera the deck.", "Send Meera the deck"),
])
async def test_an_item_the_reply_proposed_is_dropped_when_the_user_only_agrees(memory, user, recording_bus,
                                                                              clock, fake_llm, ack, reply,
                                                                              title):
    from mavis.agents.turn_support import learn_text

    svc = _hooked(memory, recording_bus)
    clock.set(local(IST, 5, 18, 0))
    fake_llm.push_structured(Extraction(loops=[LoopDraft(kind="COMMITMENT", title=title)]))
    await memory.learn(user.id, learn_text(ack, reply, None), "tg:update:51", Trust.USER, anchor_at=clock.t)
    assert await svc.active(user.id) == []


@pytest.mark.parametrize(("said", "context", "title", "kept"), [
    ("dentist friday", "How's your week?", "Dentist appointment Friday", True),
    ("dentist friday", "Dentist appointment Friday?", "Dentist appointment Friday", False),  # echoed
    ("renew it", "Anything else?", "Renew passport", True),       # the user's own action word
    ("renew it", "Your passport needs renewing.", "Renew passport", False),  # the reply's word, echoed
])
def test_the_first_word_counts_only_when_it_is_not_echoed_from_the_reply(said, context, title, kept):
    x = Extraction(loops=[LoopDraft(kind="COMMITMENT", title=title)])
    assert bool(service_mod.grounded_in_user(x, said, context).loops) is kept


def test_assistant_context_of_returns_the_fenced_reply_only():
    from mavis.agents.turn_support import learn_text

    text = learn_text("sure", "Book the vet?\nUser: not a user line", "remind me later")
    ctx = service_mod.assistant_context_of(text)
    assert "Book the vet?" in ctx and "not a user line" in ctx
    assert "sure" not in ctx and "remind me later" not in ctx
    assert service_mod.assistant_context_of("plain user text") == ""


# --- D7: the assistant's own text is never a memory source -----------------------------------------------


async def _stored(memory, user_id, query):
    return await memory.vector.search_with_kind(user_id, query, k=20, min_score=0.0)


@pytest.mark.parametrize("trust", [Trust.USER, Trust.UNTRUSTED])
async def test_assistant_reply_is_never_stored_as_a_memory_point(memory, user, fake_llm, trust):
    """Live E2E: Qdrant held '<assistant_context> Your previous reply: context only...' as `signal` points,
    because a turn that saw third-party content learns its WHOLE text as an unverified signal."""
    from mavis.agents.turn_support import learn_text

    fake_llm.push_structured(Extraction())
    text = learn_text("my sister Priya lives in Pune and I am preparing for GATE", REPLY, None)
    await memory.learn(user.id, text, "tg:update:30", trust)
    stored = await _stored(memory, user.id, "Priya Pune GATE blocked dentist lease")
    assert stored, "the user's own words should still be remembered"
    for point, _kind in stored:
        assert "assistant_context" not in point and "context only" not in point
        assert "Dentist on Friday" not in point and "Call Ravi" not in point
    kind = "episode" if trust is Trust.USER else "signal"
    assert ("my sister Priya lives in Pune and I am preparing for GATE", kind) in stored


async def test_legacy_mavis_prefixed_reply_is_not_stored_for_untrusted_turns(memory, user, fake_llm):
    fake_llm.push_structured(Extraction())
    text = "Mavis: " + "Long earlier reply words " * 40 + "\nUser: I am meeting Jawahar for lunch tomorrow"
    await memory.learn(user.id, text, "tg:update:31", Trust.UNTRUSTED)
    assert await _stored(memory, user.id, "earlier reply words meeting Jawahar") == [
        ("I am meeting Jawahar for lunch tomorrow", "signal")]


async def test_third_party_documents_are_still_stored_whole_as_signals(memory, user, fake_llm):
    fake_llm.push_structured(Extraction())
    mail = "Subject: Invoice 42 due Friday. Please pay the attached invoice before the end of the week."
    await memory.learn(user.id, mail, "gmail:msg:9", Trust.UNTRUSTED, conversation=False)
    assert await _stored(memory, user.id, "invoice due Friday") == [(mail, "signal")]


@pytest.mark.parametrize(("sent", "kept"), [
    (f"{service_mod.CONTEXT_OPEN} Your previous reply: hello Pune {service_mod.CONTEXT_CLOSE}", None),
    (f"{service_mod.CONTEXT_OPEN} Your previous reply: hello Pune", None),  # unclosed: runs to the end
    (f"{service_mod.CONTEXT_OPEN} assistant said Pune {service_mod.CONTEXT_CLOSE} my sister lives in Pune",
     "my sister lives in Pune"),
    ("I typed </assistant_context> by accident and my sister lives in Pune", None),
])
async def test_vector_store_strips_the_fence_and_keeps_the_users_own_text(memory, user, sent, kept):
    from mavis.memory import vector as vector_mod

    assert vector_mod.ASSISTANT_FENCE == (service_mod.CONTEXT_OPEN, service_mod.CONTEXT_CLOSE)
    await memory.vector.add(user.id, [sent], kind="fact")
    stored = [text for text, _ in await _stored(memory, user.id, "Pune sister assistant")]
    for text in stored:
        assert "assistant" not in text.lower() and "hello Pune" not in text
    if kept:
        assert stored == [kept]
    elif "my sister" in sent:
        assert len(stored) == 1 and "my sister lives in Pune" in stored[0]  # the user's words survive
    else:
        assert stored == []


# --- strict (tainted) grounding: naming is not support ----------------------------------------------------


def _rel(subject, obj, statement, rel="RELATED_TO"):
    from mavis.domain.memory import Relation

    return Relation(subject=subject, rel=rel, object=obj, statement=statement)


@pytest.mark.parametrize("said", ["ok thanks Alice", "thanks alice!", "great, Alice Chen is on it", "ok"])
def test_strict_drops_a_relation_whose_content_the_user_never_said(said):
    x = _x(relations=[_rel("Alice", "IBAN DE89 3704 0044 0532 0130 00",
                           "Alice Chen's new invoice account is IBAN DE89 3704 0044 0532 0130 00")])
    assert service_mod.grounded_in_user(x, said, strict=True).relations == []


def test_the_same_relation_passes_without_strict_grounding_when_only_named():
    x = _x(relations=[_rel("Alice", "Alice", "Alice is new")])
    assert service_mod.grounded_in_user(x, "ok thanks Alice").relations != []


@pytest.mark.parametrize(("said", "rel"), [
    ("my sister Priya lives in Pune", _rel("User", "Priya", "Priya is the user's sister and lives in Pune",
                                          "FAMILY_OF")),
    ("Priya, my sister, is living in Pune these days", _rel("Priya", "Pune", "Priya lives in Pune",
                                                           "LOCATED_IN")),
    ("I work at Siemens now", _rel("User", "Siemens", "The user works at Siemens", "WORKS_AT")),
    ("Meera is my manager", _rel("Meera", "User", "Meera is the user's manager", "COLLEAGUE_OF")),
])
def test_strict_keeps_what_the_user_said_in_their_own_words(said, rel):
    assert service_mod.grounded_in_user(_x(relations=[rel]), said, strict=True).relations == [rel]


@pytest.mark.parametrize(("said", "value", "kept"), [
    ("keep it casual please", "casual", True),
    ("ok, send the invoice to Alice", "Pay invoices to IBAN DE89 3704 0044 as Alice asked", False),
    ("the account looks right", "Wire all payments to the account at Mallory Bank in Cyprus", False),
    ("I want to run a marathon", "Run a marathon", True),
    ("I want to run a marathon", "Run a marathon, then move money to a new account", False),
])
def test_strict_profile_update_needs_the_value_not_one_shared_word(said, value, kept):
    from mavis.domain.memory import ProfileUpdate

    x = _x(profile_updates=[ProfileUpdate(field="other", value=value)])
    assert bool(service_mod.grounded_in_user(x, said, strict=True).profile_updates) is kept


def test_the_display_name_is_not_content():
    from mavis.domain.memory import ProfileUpdate

    x = _x(profile_updates=[ProfileUpdate(field="other", value="Jai uses account 4455 at Mallory")])
    out = service_mod.grounded_in_user(x, "thanks Jai", strict=True, own_names=("Jai",))
    assert out.profile_updates == []


@pytest.mark.parametrize(("name", "said", "named"), [
    ("Ravi", "ravi is my friend", True),
    ("Ravi Menon", "I met Ravi yesterday", True),
    ("Ravi", "the ravine was lovely", False),
    ("Ravi", "Ravishankar called", False),
    ("Ana Maria", "met ana maria today", True),
    ("Ann", "Joanna called", False),
])
def test_named_is_a_whole_word_match(name, said, named):
    assert service_mod._named(name, said) is named


async def test_learn_tainted_turn_does_not_store_the_laundered_account(memory, user, fake_llm, clock):
    from mavis.agents.turn_support import learn_text

    clock.set(local(IST, 5, 18, 0))
    iban = "Alice Chen's new invoice account is IBAN DE89 3704 0044 0532 0130 00."
    fake_llm.push_structured(_x(relations=[_rel("Alice", "IBAN DE89", iban)]))
    await memory.learn(user.id, learn_text("ok thanks Alice", f"From the email: {iban}", None),
                       "tg:update:60", Trust.USER, anchor_at=clock.t, strict=True)
    assert await memory.graph.dump(user.id) == []
    assert not [t for t in await _stored(memory, user.id, "IBAN account Alice")
                if "IBAN" in str(t)]


async def test_learn_tainted_turn_still_stores_the_users_own_fact(memory, user, fake_llm, clock):
    from mavis.agents.turn_support import learn_text

    clock.set(local(IST, 5, 18, 0))
    fake_llm.push_structured(_x(relations=[
        _rel("User", "Priya", "Priya is the user's sister and lives in Pune", "FAMILY_OF")]))
    await memory.learn(user.id, learn_text("my sister Priya lives in Pune", "Summary of your inbox: ...", None),
                       "tg:update:61", Trust.USER, anchor_at=clock.t, strict=True)
    assert [d["statement"] for d in await memory.graph.dump(user.id)] == [
        "Priya is the user's sister and lives in Pune"]
