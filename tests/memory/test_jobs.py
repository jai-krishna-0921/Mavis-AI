import asyncio

import pytest

from mavis.domain.events import Job, JobKind
from mavis.domain.memory import Entity, Extraction, Relation
from mavis.memory import jobs
from mavis.worker import locks, runner


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
    from mavis.worker.handlers import register_default_handlers

    register_default_handlers()
    assert runner._job_handlers[JobKind.LEARN] is jobs.handle_learn
    assert runner._job_handlers[JobKind.CONSOLIDATE] is jobs.handle_consolidate


async def test_handle_learn_is_idempotent_per_source_ref(memory, user, fake_llm, monkeypatch):
    async def noop(uid):
        return False

    monkeypatch.setattr(jobs, "maybe_summarize", noop)
    calls = []

    async def hook(uid, extraction, prov):
        calls.append(prov.source_ref)

    memory.on_extraction.append(hook)
    fake_llm.push_structured(Extraction())
    fake_llm.push_structured(Extraction())
    payload = {"text": "Jawahar is my good friend", "source_ref": "tg:77", "trust": "user"}
    job = Job(id="learn:tg:77", user_id=user.id, kind=JobKind.LEARN, payload=payload)
    await jobs.handle_learn(job)
    await jobs.handle_learn(job)
    assert calls == ["tg:77"]


@pytest.mark.parametrize("payload_extra,trust,conversation", [
    ({"trust": "user", "conversation": True}, "user", True),
    ({"trust": "untrusted", "conversation": True}, "untrusted", True),
    ({"trust": "untrusted", "conversation": False}, "untrusted", False),
])
async def test_handle_learn_hands_the_jobs_provenance_to_hooks(memory, user, fake_llm, monkeypatch,
                                                               payload_extra, trust, conversation):
    async def noop(uid):
        return False

    monkeypatch.setattr(jobs, "maybe_summarize", noop)
    seen = []

    async def hook(uid, extraction, prov):
        seen.append(prov)

    memory.on_extraction.append(hook)
    fake_llm.push_structured(Extraction())
    job = Job(id="learn:x:1", user_id=user.id, kind=JobKind.LEARN,
              payload={"text": "some text worth learning", "source_ref": "x:1", **payload_extra})
    await jobs.handle_learn(job)
    [prov] = seen
    assert (prov.trust.value, prov.conversation, prov.source_ref) == (trust, conversation, "x:1")
    assert len(fake_llm.structured_calls) == 1


# --- hotfix3 RC1: background LLM failures are dropped, never retried ---------------------------
import pytest  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

from mavis.bus.base import run_handler  # noqa: E402
from mavis.domain.errors import LLMError  # noqa: E402


async def test_learn_llm_failure_is_not_retried_inline_or_redelivered(memory, user, monkeypatch):
    calls = []

    async def busy_learn(*a, **k):
        calls.append(1)
        raise LLMError("timed out waiting for an LLM slot")

    monkeypatch.setattr(memory, "learn", busy_learn)
    job = Job(id="learn:tg:9", user_id=user.id, kind=JobKind.LEARN,
              payload={"text": "hello", "source_ref": "tg:9"})
    with capture_logs() as logs:
        await run_handler(jobs.handle_learn, job, what="jobs", ref=job.id)  # acked: no raise
    assert calls == [1]  # no inline retries
    assert [e["event"] for e in logs].count("memory.learn_deferred_llm_busy") == 1  # later, not now


async def test_learn_non_llm_failure_still_raises(memory, user, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("graph down")

    monkeypatch.setattr(memory, "learn", broken)
    job = Job(id="learn:tg:10", user_id=user.id, kind=JobKind.LEARN, payload={"text": "x"})
    with pytest.raises(RuntimeError):
        await jobs.handle_learn(job)


async def test_consolidate_llm_failure_is_dropped(memory, user, monkeypatch):
    async def busy(uid, mem):
        raise LLMError("LLM deadline exceeded")

    monkeypatch.setattr(jobs, "consolidate", busy)
    with capture_logs() as logs:
        await jobs.handle_consolidate(Job(id="c1", user_id=user.id, kind=JobKind.CONSOLIDATE))
    assert any(e["event"] == "memory.consolidate_dropped_llm_busy" for e in logs)


# --- hotfix3 round 1: LEARN runs after the chat grace window and retries a busy LLM twice ------------
from datetime import datetime, timedelta  # noqa: E402

from mavis.domain import timeutil  # noqa: E402
from mavis.domain.events import Event, EventType, Trust  # noqa: E402
from mavis.domain.wakeups import WakeupKind  # noqa: E402
from mavis.llm import models as llm_models  # noqa: E402
from mavis.timers.service import WakeupService  # noqa: E402


async def _learn_wakeups(user_id):
    return await WakeupService().pending(user_id, WakeupKind.SYSTEM_LEARN)


async def test_chat_learn_is_not_before_the_interactive_grace_window(user, rec_bus, clock):
    from mavis.agents.turn_support import enqueue_learn

    ev = Event(id="tg:update:1", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=clock.t,
               source="telegram", payload={"text": "hi"}, trust=Trust.USER)
    await enqueue_learn(user.id, ev, "Jawahar is my friend", None)
    [job] = [j for j in rec_bus.jobs if j.kind is JobKind.LEARN]
    not_before = datetime.fromisoformat(job.payload["not_before"])
    assert timedelta(seconds=llm_models.INTERACTIVE_GRACE_S) < not_before - clock.t <= timedelta(seconds=30)


async def test_early_learn_job_is_parked_on_a_wakeup_not_run(memory, user, monkeypatch, clock):
    async def never(*a, **k):
        raise AssertionError("must not run before not_before")

    monkeypatch.setattr(memory, "learn", never)
    due = clock.t + timedelta(seconds=28)
    payload = {"text": "x", "source_ref": "tg:2", "trust": "user", "not_before": due.isoformat()}
    await jobs.handle_learn(Job(id="learn:tg:2", user_id=user.id, kind=JobKind.LEARN, payload=payload))
    [w] = await _learn_wakeups(user.id)
    assert timeutil.ensure_utc(w.due_at) == due and w.payload["learn"]["source_ref"] == "tg:2"


async def test_learn_wakeup_enqueues_the_job_again(user, rec_bus, clock):
    payload = {"text": "x", "source_ref": "tg:3", "trust": "user", "not_before": clock.t.isoformat()}
    await jobs.on_learn_wakeup(user.id, "learn", {"learn": payload})
    [job] = [j for j in rec_bus.jobs if j.kind is JobKind.LEARN]
    assert job.payload["source_ref"] == "tg:3"


async def test_busy_learn_is_retried_twice_on_wakeups_then_dropped(memory, user, monkeypatch, clock):
    async def busy(*a, **k):
        raise LLMError("LLM slot reserved for interactive work")

    monkeypatch.setattr(memory, "learn", busy)
    payload = {"text": "x", "source_ref": "tg:4", "trust": "user"}
    await jobs.handle_learn(Job(id="learn:tg:4", user_id=user.id, kind=JobKind.LEARN, payload=payload))
    [w1] = await _learn_wakeups(user.id)
    assert timeutil.ensure_utc(w1.due_at) - clock.t == timedelta(minutes=3)
    assert w1.payload["learn"]["retry"] == 1

    await jobs.handle_learn(Job(id="learn:tg:4:r1", user_id=user.id, kind=JobKind.LEARN,
                                payload=w1.payload["learn"]))
    w2 = [w for w in await _learn_wakeups(user.id) if w.id != w1.id]
    assert [timeutil.ensure_utc(w.due_at) - clock.t for w in w2] == [timedelta(minutes=10)]
    assert w2[0].payload["learn"]["retry"] == 2

    with capture_logs() as logs:
        await jobs.handle_learn(Job(id="learn:tg:4:r2", user_id=user.id, kind=JobKind.LEARN,
                                    payload=w2[0].payload["learn"]))
    assert len(await _learn_wakeups(user.id)) == 2  # no third retry
    [final] = [e for e in logs if e["event"] == "memory.learn_dropped_final"]
    assert final["job_id"] == "learn:tg:4:r2" and final["log_level"] == "error"  # loops lost too
