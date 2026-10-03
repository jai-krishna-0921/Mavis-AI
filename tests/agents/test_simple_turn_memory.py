
from mavis.agents import simple_turn
from mavis.domain.events import JobKind
from mavis.domain.memory import Relation
from mavis.store.repo import users
from tests.agents.test_simple_turn import msg_event


async def _capture_jobs(bus, monkeypatch) -> list:
    seen = []

    async def record(job):
        seen.append(job)

    monkeypatch.setattr(bus, "enqueue", record)
    return seen


async def test_turn_injects_recall_and_enqueues_learn(db, channel, fake_llm, memory, bus, monkeypatch):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    await memory.graph.upsert_relation(user.id, Relation(subject="User", rel="FRIEND_OF", object="Jawahar",
                                                         statement="Jawahar is Jai's friend."))
    memory.invalidate(user.id)
    jobs_seen = await _capture_jobs(bus, monkeypatch)
    fake_llm.push_text("Your friend Jawahar, right?")

    await simple_turn.run_turn(msg_event(user.id, "is Jawahar free?", "tg:update:42"))

    assert "Jawahar is Jai's friend." in fake_llm.calls[-1][0].content
    [job] = jobs_seen
    assert job.kind is JobKind.LEARN and job.id == "learn:tg:update:42"
    not_before = job.payload.pop("not_before")  # parked past the LLM interactive grace window
    assert not_before
    assert job.payload.pop("anchor_at")  # when the user said it (phase A3), not when LEARN runs
    assert job.payload == {"text": "is Jawahar free?", "source_ref": "tg:update:42", "trust": "user",
                           "conversation": True}


async def test_learn_text_includes_previous_reply(db, channel, fake_llm, memory, bus, monkeypatch):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    jobs_seen = await _capture_jobs(bus, monkeypatch)
    fake_llm.push_text("What's up?")
    fake_llm.push_text("Nice.")
    await simple_turn.run_turn(msg_event(user.id, "hi", "e1"))
    await simple_turn.run_turn(msg_event(user.id, "Monday interview", "e2"))
    assert jobs_seen[1].payload["text"] == "Mavis: What's up?\nUser: Monday interview"


async def test_recall_failure_does_not_break_reply(db, channel, fake_llm, memory, bus, monkeypatch):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    jobs_seen = await _capture_jobs(bus, monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(memory, "recall", boom)
    fake_llm.push_text("Still here.")
    await simple_turn.run_turn(msg_event(user.id, "hi", "e1"))
    assert len(jobs_seen) == 1


async def test_retry_reenqueues_learn_with_same_id(db, channel, fake_llm, memory, bus, monkeypatch):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    jobs_seen = await _capture_jobs(bus, monkeypatch)
    fake_llm.push_text("Hey!")
    ev = msg_event(user.id, "hi", "e1")
    await simple_turn.run_turn(ev)
    await simple_turn.run_turn(ev)  # retry: no second LLM call
    assert len(fake_llm.calls) == 1
    assert [j.id for j in jobs_seen] == ["learn:e1", "learn:e1"]
