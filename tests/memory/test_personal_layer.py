# ruff: noqa: E501
"""Self-authored evidence, the evidence the layer rests on, and the synthesis job: varied personas, a scripted
phrasing model that sometimes misbehaves, real MemoryService and database."""

from __future__ import annotations

import pytest

from mavis.domain.events import Trust
from mavis.domain.memory import Entity, Relation
from mavis.memory import controls, personal, personal_layer, records
from mavis.memory.graph import is_self_authored, is_third_party
from mavis.store.repo import personal as personal_repo
from tests.memory.personas import (
    BODY_SERIES_A,
    DANA,
    VIKRAM,
    build_founder,
    extraction,
    founder_mailbox,
    ingest,
    mail,
    sent,
    slack_msg,
)

# --- self-authored records -----------------------------------------------------------------------------------


async def test_sent_mail_is_a_self_authored_record_of_medium_trust_never_user(founder):
    sink = await ingest(founder, mails=[sent(founder, "f-s1", VIKRAM, "Series A deck", BODY_SERIES_A)])
    (job,) = sink.jobs
    assert job.authored and job.trust == "medium" and job.source_ref == "gmail:f-s1"
    assert job.label == "email you sent"
    payload = job.payload()
    assert payload["trust"] == Trust.UNTRUSTED.value and payload["record"]["authored"] is True
    assert next(p for p in job.people if p.role == "sender").is_user


async def test_learning_a_sent_mail_writes_self_authored_facts_that_never_outrank_what_the_user_said(founder, memory, phraser):
    ph = phraser(extractions=[extraction(org="Northwind", role_statement="Anita is the CEO of Northwind.", project="Series A deck")])
    # the user once told Mavis where they work: that must survive any record
    await memory.graph.upsert_entity(founder.user_id, Entity(name="Northwind Labs", label="Organization"))
    await memory.graph.upsert_relation(founder.user_id, Relation(subject="User", rel="WORKS_AT", object="Northwind Labs", statement="You work at Northwind Labs."), source_ref="chat:1")
    (job,) = (await ingest(founder, mails=[sent(founder, "f-s1", VIKRAM, "Series A deck", BODY_SERIES_A)])).jobs
    await records.learn_record(memory, founder.user_id, job.payload())
    dump = await memory.graph.dump(founder.user_id)
    works = [d for d in dump if d["relation"] == "WORKS_AT"]
    assert [d["object"] for d in works] == ["Northwind Labs"] and works[0]["source_ref"] == "chat:1"  # the user's word stands
    about = [d for d in dump if d["relation"] == "ABOUT"]
    assert about and all(is_self_authored(d["source_ref"]) and not is_third_party(d["source_ref"]) for d in about)
    assert "Series A deck" in about[0]["object"]
    assert ph.extractions == []


async def test_authored_fact_in_an_empty_graph_keeps_its_single_valued_relation(founder, memory, phraser):
    phraser(extractions=[extraction(org="Northwind", role_statement="Anita is the CEO of Northwind.")])
    (job,) = (await ingest(founder, mails=[sent(founder, "f-s1", VIKRAM, "Series A deck", BODY_SERIES_A)])).jobs
    await records.learn_record(memory, founder.user_id, job.payload())
    works = [d for d in await memory.graph.dump(founder.user_id) if d["relation"] == "WORKS_AT"]
    assert works and works[0]["source_ref"] == "sa:gmail:f-s1"


async def test_a_mail_from_the_users_address_that_is_not_theirs_is_third_party(founder):
    forged = mail("forged", founder.address, "Dana Whitfield <dana@brightcap.vc>", "Wire", "Please wire the money to the new account today, thanks a lot.")
    sink = await ingest(founder, mails=[forged])
    assert all(not j.authored for j in sink.jobs)


async def test_authenticated_mail_from_the_user_address_counts_as_their_own(founder):
    echo = mail("echo", founder.address, VIKRAM, "Note to self", "Remember to review the term sheet before the board meeting on Friday.", auth=True)
    (job,) = (await ingest(founder, mails=[echo])).jobs
    assert job.authored


async def test_own_slack_messages_are_authored_and_colleagues_messages_are_not(engineer):
    mine = slack_msg(engineer.slack_id, "I am rolling out the Atlas gateway migration to staging tonight, ping me if you see errors", ts="1790000001.000100")
    theirs = slack_msg("U0LENA0001", "Can you review the Atlas gateway migration plan before standup tomorrow?", ts="1790000002.000100", user_name="Lena Fischer")
    sink = await ingest(engineer, slacks=[mine, theirs])
    by_ref = {j.source_ref.rsplit(":", 1)[-1]: j for j in sink.jobs}
    assert by_ref["1790000001.000100"].authored and by_ref["1790000001.000100"].label == "Slack message you wrote"
    assert not by_ref["1790000002.000100"].authored


async def test_redaction_still_applies_to_the_users_own_words(student):
    body = "Hi Prof, my roll number is fine but please note the card for the fee is 4242 4242 4242 4242 and the account number is 912010034455667 for the refund."
    (job,) = (await ingest(student, mails=[sent(student, "t-s1", "Prof Ramanathan <ramanathan@iitm.ac.in>", "Fee refund", "Professor, " + body)])).jobs
    assert "4242 4242 4242 4242" not in job.text and "912010034455667" not in job.text


async def test_muted_recipients_and_paused_connectors_teach_nothing(founder):
    from mavis.tools.integrations.native import guard

    await guard.add_mute(founder.user_id, "dana@brightcap.vc")
    s1 = sent(founder, "f-m1", DANA, "Term sheet", "Dana, thanks for the term sheet, we have a few questions on the liquidation preference.")
    sink = await ingest(founder, mails=[s1])
    assert sink.jobs == [] and await personal_repo.signals(founder.user_id, "interaction") == []
    await controls.set_paused(founder.user_id, "gmail", True)
    sink = await ingest(founder, mails=[sent(founder, "f-m2", VIKRAM, "Deck", BODY_SERIES_A)])
    assert sink.jobs == [] and await personal_repo.signals(founder.user_id, "interaction") == []
    await controls.set_paused(founder.user_id, "gmail", False)
    assert len((await ingest(founder, mails=[sent(founder, "f-m3", VIKRAM, "Deck", BODY_SERIES_A)])).jobs) == 1


async def test_the_daily_learn_budget_limits_extractions_but_not_the_style_measure(founder, monkeypatch):
    monkeypatch.setattr(controls, "AUTHORED_LEARN_PER_DAY", 2)
    mails = [sent(founder, f"f-b{i}", VIKRAM, f"Update {i}", f"Vikram, update number {i} on the hiring plan and the board deck is attached for review today.") for i in range(5)]
    sink = await ingest(founder, mails=mails)
    assert len(sink.jobs) == 2
    assert len(await personal_repo.signals(founder.user_id, "style")) == 5


async def test_short_replies_feed_style_but_are_not_extracted(student):
    sink = await ingest(student, mails=[sent(student, "t-s2", "Priya Nair <priya@students.iitm.ac.in>", "Re: notes", "ok thanks!")])
    assert sink.jobs == [] and len(await personal_repo.signals(student.user_id, "style")) == 1


async def test_the_same_record_twice_counts_once(founder):
    m = sent(founder, "f-s1", VIKRAM, "Series A deck", BODY_SERIES_A)
    await ingest(founder, mails=[m, m])
    sigs = await personal_repo.signals(founder.user_id, "interaction")
    assert len(sigs) == 1 and len(await personal_repo.signals(founder.user_id, "style")) == 1


# --- evidence computed in code --------------------------------------------------------------------------------


async def test_people_are_ranked_by_frequency_and_recency_with_exact_counts(founder, memory):
    await ingest(founder, mails=founder_mailbox(founder))
    people = await personal.aggregate_people(founder.user_id, personal.timeutil.now())
    names = [(a.display, dict(a.counts)) for a in people]
    assert names[0][0] == "Vikram Shah" and names[0][1] == {"sent": 3}
    dana = next(a for a in people if a.display == "Dana Whitfield")
    assert dict(dana.counts) == {"sent": 1, "received": 1}
    ev = {e.id: e for e in await personal.gather(memory, founder.user_id)}
    vik = ev["person:vikram@northwind.io"]
    assert "3 sent" in vik.text and vik.tier == "self"
    raj = ev["person:raj@consultco.biz"]
    assert "3 received" in raj.text and raj.tier == "third"  # only they wrote: unconfirmed


async def test_a_stale_contact_ranks_below_a_recent_one_with_the_same_count(student, memory):
    from mavis.domain import timeutil

    await personal_repo.record_signal(student.user_id, "interaction", "old@uni.edu", "gmail:a", "Old Friend", timeutil.now() - __import__("datetime").timedelta(days=80), {"dir": "sent", "name": "Old Friend", "email": "old@uni.edu"})
    await personal_repo.record_signal(student.user_id, "interaction", "new@uni.edu", "gmail:b", "New Friend", timeutil.now() - __import__("datetime").timedelta(days=1), {"dir": "sent", "name": "New Friend", "email": "new@uni.edu"})
    people = await personal.aggregate_people(student.user_id, timeutil.now())
    assert [a.display for a in people] == ["New Friend", "Old Friend"]


async def test_automated_senders_are_never_key_people(founder, memory):
    for i in range(4):
        await ingest(founder, mails=[mail(f"n{i}", "Billing <billing@saas.com>", founder.address, f"Invoice {i}", "Your invoice is attached to this message.", at=f"2026-09-2{i}T09:00:00Z")])
    ev = await personal.gather(memory, founder.user_id)
    assert not [e for e in ev if e.kind == "person"]


async def test_recurring_meetings_come_from_the_calendar_in_code(engineer, memory):
    from datetime import UTC, datetime

    mondays = [datetime(2026, 9, d, 4, 0, tzinfo=UTC) for d in (7, 14, 21, 28)]  # 09:30 IST every Monday
    for i, start in enumerate(mondays):
        await personal.note_meeting(engineer.user_id, title="Platform standup", start=start, attendees=["lena@northwind.io"], tz="Asia/Kolkata", event_id=f"e{i}")
    await personal.note_meeting(engineer.user_id, title="Dentist", start=datetime(2026, 9, 30, 5, 0, tzinfo=UTC), attendees=[], tz="Asia/Kolkata", event_id="d1")
    routines = [e for e in await personal.gather(memory, engineer.user_id) if e.section == "routines"]
    assert len(routines) == 1 and "weekly" in routines[0].text and "Mondays at 09:30" in routines[0].text and "4 occurrences" in routines[0].text
    lena = next(e for e in await personal.gather(memory, engineer.user_id) if e.id == "person:lena@northwind.io")
    assert "4 shared meetings" in lena.text and lena.tier == "self"  # the user's own calendar


async def test_writing_style_is_measured_not_guessed(student, engineer, memory):
    for i in range(6):
        await ingest(student, mails=[sent(student, f"t-{i}", "Priya Nair <priya@students.iitm.ac.in>", "notes", "hey, lol can u send the notes yaar, thx a lot, will return them tmrw " * 1)])
    long_body = " ".join(["Please find the detailed architecture review attached for your consideration and comments before the release."] * 5)
    for i in range(6):
        await ingest(engineer, mails=[sent(engineer, f"d-{i}", VIKRAM, "Architecture", long_body)])
    (s,) = [e for e in await personal.gather(memory, student.user_id) if e.section == "style"]
    (d,) = [e for e in await personal.gather(memory, engineer.user_id) if e.section == "style"]
    assert "short" in s.text and "casual" in s.text and "Latin" in s.text
    assert "long" in d.text and "casual" not in d.text


async def test_language_mix_reads_the_scripts_the_user_writes(student, memory):
    for i in range(5):
        await ingest(student, mails=[sent(student, f"hi-{i}", "Meera Joshi <meera@students.iitm.ac.in>", "plan", "कल की क्लास के बाद hum library mein milenge, notes laana please और assignment भी")])
    (s,) = [e for e in await personal.gather(memory, student.user_id) if e.section == "style"]
    assert "Devanagari" in s.text and "Latin" in s.text


async def test_user_trust_beats_derived_in_a_one_answer_slot(founder, memory):
    """They told Mavis they work at Northwind Labs; a record says Zeus. Only the user's statement is evidence."""
    uid = founder.user_id
    await memory.graph.upsert_entity(uid, Entity(name="Zeus Corp", label="Organization"))
    await memory.graph.upsert_relation(uid, Relation(subject="User", rel="WORKS_AT", object="Zeus Corp", statement="The user works at Zeus Corp."), source_ref="sa:gmail:x")
    ev = [e for e in await personal.gather(memory, uid) if e.section == "identity"]
    assert [e.text for e in ev] == ["The user works at Zeus Corp."] and ev[0].tier == "self"
    await memory.graph.upsert_entity(uid, Entity(name="Northwind Labs", label="Organization"))
    await memory.graph.upsert_relation(uid, Relation(subject="User", rel="WORKS_AT", object="Northwind Labs", statement="You work at Northwind Labs."), source_ref="chat:9")
    ev = [e for e in await personal.gather(memory, uid) if e.section == "identity"]
    assert [e.text for e in ev] == ["You work at Northwind Labs."] and ev[0].tier == "user"


# --- synthesis ------------------------------------------------------------------------------------------------


async def test_every_line_maps_to_evidence_and_unconfirmed_lines_are_marked(founder, memory, phraser):
    ph, layer = await build_founder(founder, memory, phraser)
    ids = {e.id for e in await personal.gather(memory, founder.user_id)}
    assert layer["phrased"] and layer["lines"]
    for ln in layer["lines"]:
        assert ln["evidence"] and set(ln["evidence"]) <= ids and ln["sources"] is not None
    by = {tuple(ln["evidence"]): ln for ln in layer["lines"]}
    raj = by[("person:raj@consultco.biz",)]
    assert raj["confirmed"] is False and raj["tier"] == "third"
    vik = by[("person:vikram@northwind.io",)]
    assert vik["confirmed"] is True and "3 sent" in vik["text"]
    # third-party evidence reached the model only inside the untrusted wrapper
    assert "<untrusted" in ph.prompts[0]
    identity = [ln for ln in layer["lines"] if ln["section"] == "identity"]
    assert any("CEO of Northwind" in ln["text"] for ln in identity)


@pytest.mark.parametrize("mode", ["invent", "bad_ids", "cross_section", "mixed", "empty"])
async def test_what_the_model_invents_or_mislabels_never_reaches_the_layer(founder, memory, phraser, mode):
    _, layer = await build_founder(founder, memory, phraser, mode)
    texts = " ".join(ln["text"] for ln in layer["lines"])
    assert "Globex" not in texts and "CTO" not in texts
    evidence = {e.id: e for e in await personal.gather(memory, founder.user_id)}
    for ln in layer["lines"]:
        cited = [evidence[i] for i in ln["evidence"]]
        assert all(e.section == ln["section"] for e in cited)
        assert len({e.tier == "third" for e in cited}) == 1
        assert personal_layer.supported_by(ln["text"], cited)
    assert layer["lines"], "sections the model failed on fall back to the evidence text"


async def test_salient_check_catches_names_numbers_and_accepts_plurals():
    ev = [personal.Evidence(id="x", kind="person", section="people", text="Vikram Shah: 3 sent in the last 90 days", tier="self")]
    assert personal_layer.supported_by("Vikram Shah: 3 sent", ev)
    assert not personal_layer.supported_by("You emailed Vikram Shah 12 times", ev)
    assert not personal_layer.supported_by("Vikram Shah of Globex", ev)


async def test_unchanged_evidence_makes_no_new_version_and_no_model_call(founder, memory, phraser):
    ph, first = await build_founder(founder, memory, phraser)
    calls = len(ph.prompts)
    assert await personal_layer.build(memory, founder.user_id) is None
    assert len(ph.prompts) == calls
    assert (await personal_layer.current(founder.user_id))["version"] == first["version"]


async def test_a_busy_model_keeps_the_old_layer_and_a_plain_one_is_built_when_none_exists(founder, memory, phraser):
    phraser("error")
    await ingest(founder, mails=founder_mailbox(founder))
    from mavis.domain.errors import LLMError

    with pytest.raises(LLMError):
        await personal_layer.build(memory, founder.user_id)
    assert (await personal_layer.current(founder.user_id))["version"] == 0
    plain = await personal_layer.build_plain(memory, founder.user_id)
    assert plain and not plain["phrased"] and any("3 sent" in ln["text"] for ln in plain["lines"])


async def test_the_wakeup_builds_the_layer_and_schedules_the_next_one(founder, memory, phraser):
    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import wakeups as wakeups_repo

    phraser(extractions=[])
    await ingest(founder, mails=founder_mailbox(founder))
    await personal_layer.on_wakeup(founder.user_id, "personal layer", {"mode": "soon"})
    assert (await personal_layer.current(founder.user_id))["version"] == 1
    pending = await wakeups_repo.list_pending(founder.user_id, WakeupKind.SYSTEM_PERSONAL_LAYER)
    assert [w.payload.get("mode") for w in pending] == ["daily"]
    await personal_layer.schedule(founder.user_id)  # requests coalesce into one
    await personal_layer.schedule(founder.user_id)
    pending = await wakeups_repo.list_pending(founder.user_id, WakeupKind.SYSTEM_PERSONAL_LAYER)
    assert sorted(w.payload.get("mode") for w in pending) == ["daily", "soon"]


async def test_a_busy_model_retries_the_wakeup_a_few_times_then_builds_plain(founder, memory, phraser):
    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import wakeups as wakeups_repo

    phraser("error")
    await ingest(founder, mails=founder_mailbox(founder))
    await personal_layer.on_wakeup(founder.user_id, "personal layer", {"mode": "soon"})
    (retry,) = [w for w in await wakeups_repo.list_pending(founder.user_id, WakeupKind.SYSTEM_PERSONAL_LAYER) if w.payload.get("mode") == "retry"]
    assert retry.payload["retry"] == 1
    await personal_layer.on_wakeup(founder.user_id, "personal layer", {"mode": "retry", "retry": personal_layer.MAX_RETRIES})
    assert (await personal_layer.current(founder.user_id))["version"] == 1  # plain layer, never empty-handed


async def test_calendar_events_from_the_poller_become_occurrences(engineer, memory):
    from datetime import UTC, datetime

    from mavis.domain.events import Event, EventType

    for i, d in enumerate((7, 14, 21)):
        ev = Event(id=f"gcal:{i}", user_id=engineer.user_id, type=EventType.CALENDAR_CHANGED, occurred_at=datetime(2026, 9, d, tzinfo=UTC), source="g",
                   payload={"event_id": f"e{i}", "title": "Platform standup", "start": datetime(2026, 9, d, 4, 0, tzinfo=UTC).isoformat(), "attendees": []}, trust=Trust.UNTRUSTED)
        await personal_layer.on_calendar_event(ev)
    assert len([e for e in await personal.meeting_evidence(engineer.user_id)]) == 1
    await controls.set_paused(engineer.user_id, "calendar", True)
    await personal_layer.on_calendar_event(Event(id="gcal:9", user_id=engineer.user_id, type=EventType.CALENDAR_CHANGED, occurred_at=datetime(2026, 9, 28, tzinfo=UTC), source="g",
                                                payload={"event_id": "e9", "title": "Planning", "start": datetime(2026, 9, 28, 4, 0, tzinfo=UTC).isoformat()}, trust=Trust.UNTRUSTED))
    assert len(await personal_repo.signals(engineer.user_id, "meeting")) == 3
