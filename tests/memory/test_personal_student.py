# ruff: noqa: E501
"""A student: no employer, mostly short casual messages, a thesis, professors who write in. Different shapes than
the founder and engineer, same rules."""

from __future__ import annotations

from mavis.domain.memory import Entity, Extraction, Relation
from mavis.memory import personal, personal_layer, records
from mavis.memory.personal_layer import ensure_scheduled
from mavis.memory.vault import Vault
from tests.memory.personas import ingest, learn_all, mail, sent

PROF = "Prof Ramanathan <ramanathan@iitm.ac.in>"


def thesis_extraction():
    return Extraction(
        entities=[Entity(name="IIT Madras", label="Organization"), Entity(name="thesis", label="Project")],
        relations=[Relation(subject="User", rel="STUDIES_AT", object="IIT Madras", statement="Tara studies at IIT Madras."),
                   Relation(subject="User", rel="ABOUT", object="thesis", statement="Tara is writing her thesis.")])


async def mailbox(student):
    mails = [sent(student, "t-1", PROF, "Thesis chapter", "Professor, I have attached the second chapter of my thesis for your comments before the review at IIT Madras next week.", "2026-09-26T09:00:00Z"),
             sent(student, "t-2", PROF, "Meeting", "Professor, could we meet on Thursday afternoon to discuss the thesis chapter and the experiments?", "2026-09-27T09:00:00Z"),
             sent(student, "t-3", "Priya Nair <priya@students.iitm.ac.in>", "notes", "hey lol can u send the thesis notes yaar, will return them tmrw morning, thx a lot", "2026-09-27T12:00:00Z"),
             mail("t-4", PROF, student.address, "Re: Thesis chapter", "Thank you, I will read the thesis chapter this week and send comments.", auth=True, at="2026-09-28T09:00:00Z"),
             mail("t-5", "Placement Cell <placements@iitm.ac.in>", student.address, "Drive", "Campus drive on Friday, register on the portal before Thursday evening.", at="2026-09-28T10:00:00Z")]
    return mails


async def test_a_student_layer_has_studies_at_identity_and_confirmed_professor(student, memory, phraser):
    ph = phraser(extractions=[thesis_extraction()] * 8)
    sink = await ingest(student, mails=await mailbox(student))
    await learn_all(memory, sink)
    layer = await personal_layer.build(memory, student.user_id)
    identity = [ln for ln in layer["lines"] if ln["section"] == "identity"]
    assert identity and "studies at IIT Madras" in identity[0]["text"] and identity[0]["confirmed"]
    people = {tuple(ln["evidence"]): ln for ln in layer["lines"] if ln["section"] == "people"}
    prof = people[("person:ramanathan@iitm.ac.in",)]
    assert prof["confirmed"] and "2 sent" in prof["text"] and "1 received" in prof["text"]
    assert ("person:placements@iitm.ac.in",) not in people  # a single bulk-ish sender is not a key person
    style = [ln for ln in layer["lines"] if ln["section"] == "style"]
    assert style == [] or "short" in style[0]["text"]  # three own messages: below the measure threshold
    assert ph.prompts


async def test_a_student_with_no_workplace_never_gets_one_invented(student, memory, phraser):
    phraser(extractions=[thesis_extraction()] * 8)
    await learn_all(memory, await ingest(student, mails=await mailbox(student)))
    layer = await personal_layer.build(memory, student.user_id)
    text = " ".join(ln["text"] for ln in layer["lines"])
    assert "works at" not in text.lower() and "Northwind" not in text


async def test_corrections_to_identity_replace_the_derived_line_for_good(student, memory, phraser):
    phraser(extractions=[thesis_extraction()] * 8)
    await learn_all(memory, await ingest(student, mails=await mailbox(student)))
    layer = await personal_layer.build(memory, student.user_id)
    vault = Vault(memory)
    old = next(ln for ln in layer["lines"] if ln["section"] == "identity")
    await vault.correct_item(student.user_id, old["id"], "I am a master's student in robotics at IIT Madras.")
    for _ in range(2):  # a rebuild, then new evidence and another rebuild: the correction holds
        rebuilt = await personal_layer.build(memory, student.user_id, force=True)
        assert [ln["text"] for ln in rebuilt["lines"] if ln["section"] == "identity"] == ["I am a master's student in robotics at IIT Madras."]
        await learn_all(memory, await ingest(student, mails=[sent(student, f"t-x{_}", PROF, "More", "Professor, here is another draft of the thesis chapter for your comments at IIT Madras, thank you.")]))


async def test_startup_schedules_a_daily_rebuild_for_every_user_once(student, engineer):
    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import wakeups as wakeups_repo

    await ensure_scheduled()
    await ensure_scheduled()
    for p in (student, engineer):
        pending = await wakeups_repo.list_pending(p.user_id, WakeupKind.SYSTEM_PERSONAL_LAYER)
        assert [w.payload.get("mode") for w in pending] == ["daily"]


def test_helpers_are_importable():
    assert personal.SECTIONS and records.GUIDANCE


async def test_old_interactions_stop_counting_and_are_pruned_by_the_daily_job(student, memory, phraser):
    from datetime import timedelta

    from mavis.domain import timeutil
    from mavis.store.repo import personal as personal_repo

    phraser(extractions=[])
    old = timeutil.now() - timedelta(days=200)
    await personal_repo.record_signal(student.user_id, "interaction", "old@uni.edu", "gmail:o", "Old", old, {"dir": "sent", "name": "Old", "email": "old@uni.edu"})
    await personal_repo.record_signal(student.user_id, "interaction", "new@uni.edu", "gmail:n", "New", timeutil.now(), {"dir": "sent", "name": "New", "email": "new@uni.edu"})
    assert [a.key for a in await personal.aggregate_people(student.user_id, timeutil.now())] == ["new@uni.edu"]
    await personal_layer.on_wakeup(student.user_id, "personal layer", {"mode": "daily"})
    assert [s.key for s in await personal_repo.signals(student.user_id, "interaction")] == ["new@uni.edu"]
