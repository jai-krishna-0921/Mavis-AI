import asyncio

from zento.domain.events import Job, JobKind
from zento.domain.memory import Entity, Extraction, Relation
from zento.memory import jobs
from zento.worker import locks, runner


async def test_handle_learn_runs_learn_and_summary(memory, user, fake_llm, monkeypatch):
    summarised = []

    async def fake_summarize(uid):
        summarised.append(uid)
        return False

    monkeypatch.setattr(jobs, "maybe_summarize", fake_summarize)
    fake_llm.push_structured(Extraction(
        entities=[Entity(name="Jawahar", label="Person")],
        relations=[Relation(subject="User", rel="FRIEND_OF", object="Jawahar",
                            statement="Jawahar is Jai's friend.")],
    ))
    payload = {"text": "Jawahar is my friend", "source_ref": "tg:1", "trust": "user"}
    await jobs.handle_learn(Job(id="learn:tg:1", user_id=user.id, kind=JobKind.LEARN, payload=payload))
    assert await memory.graph.dump(user.id)
    assert summarised == [user.id]


async def test_handle_learn_skips_summary_for_signals(memory, user, fake_llm, monkeypatch):
    async def never(uid):
        raise AssertionError("signals are not conversation")

    monkeypatch.setattr(jobs, "maybe_summarize", never)
    fake_llm.push_structured(Extraction())
    payload = {"text": "security alert", "source_ref": "gmail:1", "trust": "untrusted", "conversation": False}
    await jobs.handle_learn(Job(id="learn:gmail:1", user_id=user.id, kind=JobKind.LEARN, payload=payload))


async def test_learn_and_consolidate_are_serialised_per_user_off_the_turn_lock(memory, user, monkeypatch):
    active, overlap = 0, False

    async def slow_learn(*a, **k):
        nonlocal active, overlap
        active += 1
        overlap = overlap or active > 1
        await asyncio.sleep(0.02)
        active -= 1

    async def slow_consolidate(uid, mem):
        await slow_learn()

    monkeypatch.setattr(memory, "learn", slow_learn)
    monkeypatch.setattr(jobs, "maybe_summarize", lambda uid: asyncio.sleep(0))
    monkeypatch.setattr(jobs, "consolidate", slow_consolidate)
    learn = Job(id="l", user_id=user.id, kind=JobKind.LEARN, payload={"text": "x"})
    cons = Job(id="c", user_id=user.id, kind=JobKind.CONSOLIDATE, payload={})
    async with locks.user_lock(user.id):  # a running turn must not block jobs
        run = asyncio.gather(
            jobs.handle_learn(learn), jobs.handle_consolidate(cons), jobs.handle_learn(learn)
        )
        await asyncio.wait_for(run, 5)
    assert not overlap


async def test_register_wires_both_kinds(monkeypatch):
    registered = {}
    monkeypatch.setattr(jobs, "register_job_handler", lambda kind, fn: registered.__setitem__(kind, fn))
    jobs.register()
    assert registered[JobKind.LEARN] is jobs.handle_learn
    assert registered[JobKind.CONSOLIDATE] is jobs.handle_consolidate


async def test_default_handlers_register_memory_jobs():
    from zento.worker.handlers import register_default_handlers

    register_default_handlers()
    assert runner._job_handlers[JobKind.LEARN] is jobs.handle_learn
    assert runner._job_handlers[JobKind.CONSOLIDATE] is jobs.handle_consolidate
