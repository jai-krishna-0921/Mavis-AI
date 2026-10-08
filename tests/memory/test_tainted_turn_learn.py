"""E2E run 2 (R3): the user's own words are theirs even in a turn that read third-party output. A tainted
turn used to be learned as untrusted, so "I'm Arjun" never reached the profile and "my sister Priya lives in
Pune" never reached the graph (it stayed a 'signal'). Taint now tightens grounding instead."""

from __future__ import annotations

import pytest

from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.domain.memory import Entity, Extraction, ProfileUpdate, Relation
from mavis.memory import jobs as memory_jobs
from mavis.store.repo import profile as profile_repo


class _Bus:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def enqueue(self, job: Job) -> None:
        self.jobs.append(job)


async def _enqueue(monkeypatch, user_id, text, *, tainted, trust=Trust.USER, previous=None) -> Job:
    import mavis.agents.turn_support as ts

    bus = _Bus()
    monkeypatch.setattr(ts, "get_bus", lambda: bus)
    ev = Event(id="tg:update:77", user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
               source="telegram", payload={"text": text}, trust=trust)
    await ts.enqueue_learn(user_id, ev, text, previous, tainted=tainted)
    return bus.jobs[-1]


@pytest.mark.parametrize("tainted", [True, False])
async def test_a_users_words_keep_user_trust_whatever_the_turn_read(user, monkeypatch, tainted):
    job = await _enqueue(monkeypatch, user.id, "I'm Arjun", tainted=tainted)
    assert job.payload["trust"] == "user" and job.payload["tainted"] is tainted


async def test_an_untrusted_event_stays_untrusted(user, monkeypatch):
    job = await _enqueue(monkeypatch, user.id, "forwarded text", tainted=False, trust=Trust.UNTRUSTED)
    assert job.payload["trust"] == "untrusted"


def _extraction(*, name="Arjun", sister="Priya", city="Pune") -> Extraction:
    return Extraction(
        entities=[Entity(name=sister, label="Person"), Entity(name=city, label="Place")],
        relations=[
            Relation(subject="User", rel="FAMILY_OF", object=sister,
                     statement=f"{sister} is the user's sister."),
            Relation(subject=sister, rel="LOCATED_IN", object=city, statement=f"{sister} lives in {city}."),
        ],
        profile_updates=[ProfileUpdate(field="name", value=name)],
    )


async def _run_learn(user_id, text, *, tainted):
    await memory_jobs.handle_learn(Job(
        id="learn:x", user_id=user_id, kind=JobKind.LEARN,
        payload={"text": text, "source_ref": "tg:update:77", "trust": "user", "conversation": True,
                 "tainted": tainted, "anchor_at": timeutil.now().isoformat()}))


@pytest.mark.parametrize("previous", [None, "Here are 3 desks from the web."])
async def test_tainted_turn_still_fills_graph_and_profile(memory, user, fake_llm, monkeypatch, previous):
    from mavis.agents.turn_support import learn_text
    from mavis.memory import service as service_mod

    service_mod.set_memory(memory)
    try:
        said = "I'm Arjun, my sister Priya lives in Pune"
        fake_llm.push_structured(_extraction())
        await _run_learn(user.id, learn_text(said, previous, None), tainted=True)
    finally:
        service_mod.set_memory(None)
    assert {"Priya", "Pune"} <= {e.name for e in await memory.graph.entities(user.id)}
    assert (await profile_repo.get(user.id)).name == "Arjun"
    facts = [d["statement"] for d in await memory.graph.dump(user.id)]
    assert "Priya lives in Pune." in facts


async def test_tainted_turn_drops_items_the_user_never_said(memory, user, fake_llm):
    """Taint makes grounding strict even with no reply included: a name the model lifted from third-party
    text (not the user's words) never reaches the graph or the profile."""
    from mavis.memory import service as service_mod

    service_mod.set_memory(memory)
    try:
        fake_llm.push_structured(_extraction(name="Mallory", sister="Mallory", city="Lagos"))
        await _run_learn(user.id, "I'm fine, thanks", tainted=True)
    finally:
        service_mod.set_memory(None)
    assert (await profile_repo.get(user.id)).name is None
    assert not any(d["statement"].startswith("Mallory") for d in await memory.graph.dump(user.id))


@pytest.mark.parametrize("name", ["Priya", "प्रिया", "பிரியா", "José", "Zoë", "Mary-Anne", "O'Brien"])
async def test_non_ascii_and_punctuated_names_the_user_wrote_are_kept(memory, user, fake_llm, name):
    from mavis.memory import service as service_mod

    service_mod.set_memory(memory)
    try:
        fake_llm.push_structured(_extraction(sister=name))
        await _run_learn(user.id, f"my sister {name} lives in Pune", tainted=True)
    finally:
        service_mod.set_memory(None)
    assert any(name in d["statement"] for d in await memory.graph.dump(user.id))
