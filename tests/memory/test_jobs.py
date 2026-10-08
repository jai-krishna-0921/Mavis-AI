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
from mavis.timers.service import WakeupService  # noqa: E402


async def _learn_wakeups(user_id):
    return await WakeupService().pending(user_id, WakeupKind.SYSTEM_LEARN)


async def test_chat_learn_is_queued_shortly_after_the_turn_not_after_a_grace_window(user, rec_bus, clock):
    """The limiter queues best_effort work now, so LEARN needs no long delay (it used to wait out the
    20 s grace window and was then refused anyway)."""
    from mavis.agents.turn_support import enqueue_learn

    ev = Event(id="tg:update:1", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=clock.t,
               source="telegram", payload={"text": "hi"}, trust=Trust.USER)
    await enqueue_learn(user.id, ev, "Jawahar is my friend", None)
    [job] = [j for j in rec_bus.jobs if j.kind is JobKind.LEARN]
    not_before = datetime.fromisoformat(job.payload["not_before"])
    assert timedelta(0) <= not_before - clock.t <= timedelta(seconds=10)


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


def _job(job_id, user_id, payload):
    return Job(id=job_id, user_id=user_id, kind=JobKind.LEARN, payload=payload)


async def _fail_learn(memory, monkeypatch):
    async def busy(*a, **k):
        raise LLMError("LLM slot reserved for interactive work")

    monkeypatch.setattr(memory, "learn", busy)


async def test_busy_learn_is_retried_with_growing_then_capped_delay(memory, user, monkeypatch, clock):
    await _fail_learn(memory, monkeypatch)
    payload = {"text": "x", "source_ref": "tg:4", "trust": "user"}
    delays = []
    seen = set()
    for n in range(8):
        await jobs.handle_learn(_job(f"learn:tg:4:w{n}", user.id, payload))
        [w] = [w for w in await _learn_wakeups(user.id) if w.id not in seen]
        seen.add(w.id)
        delays.append(timeutil.ensure_utc(w.due_at) - clock.t)
        assert w.payload["learn"]["retry"] == n + 1
        payload = w.payload["learn"]
    assert delays[:3] == [timedelta(minutes=1), timedelta(minutes=3), timedelta(minutes=10)]
    assert max(delays) == jobs.LEARN_RETRY_CAP  # then a steady cadence: bounded delay, never dropped
    assert delays[-1] == jobs.LEARN_RETRY_CAP


async def test_learn_survives_many_failures_but_is_dropped_after_a_day(memory, user, monkeypatch, clock):
    await _fail_learn(memory, monkeypatch)
    first = clock.t - timedelta(hours=3)
    payload = {"text": "x", "source_ref": "tg:5", "trust": "user", "retry": 40, "first_at": first.isoformat()}
    await jobs.handle_learn(_job("learn:tg:5", user.id, payload))
    assert len(await _learn_wakeups(user.id)) == 1  # 40 failures in, still queued

    stale = clock.t - jobs.LEARN_MAX_AGE - timedelta(minutes=1)
    old = {**payload, "source_ref": "tg:6", "first_at": stale.isoformat()}
    with capture_logs() as logs:
        await jobs.handle_learn(_job("learn:tg:6", user.id, old))
    assert len(await _learn_wakeups(user.id)) == 1  # nothing new parked
    [final] = [e for e in logs if e["event"] == "memory.learn_dropped_final"]
    assert final["log_level"] == "error"


async def test_retried_learn_eventually_succeeds_once(memory, user, fake_llm, monkeypatch, clock):
    """Fail twice, then the model answers: the facts are stored once, the job is marked seen."""
    from mavis.store.repo import events

    calls = {"n": 0}
    real = memory.learn

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise LLMError("timed out waiting for an LLM slot")
        return await real(*a, **k)

    async def no_summary(uid):
        return False

    monkeypatch.setattr(memory, "learn", flaky)
    monkeypatch.setattr(jobs, "maybe_summarize", no_summary)
    fake_llm.push_structured(Extraction(
        entities=[Entity(name="Priya", label="Person")],
        relations=[Relation(subject="User", rel="SIBLING_OF", object="Priya",
                            statement="Priya is the user's sister.")],
    ))
    payload = {"text": "Priya is my sister", "source_ref": "tg:7", "trust": "user"}
    for n in range(3):
        await jobs.handle_learn(_job(f"learn:tg:7:w{n}", user.id, payload))
        parked = await _learn_wakeups(user.id)
        if n < 2:
            payload = max(parked, key=lambda w: w.id).payload["learn"]
    assert calls["n"] == 3 and await events.seen("learn:tg:7")
    assert len(await memory.graph.dump(user.id)) == 1


async def test_deferred_learn_keeps_source_time_end_to_end(memory, user, fake_llm, monkeypatch, clock):
    """Pune said first (LEARN fails and is retried later), Delhi said second (applied at once): after the
    retry Delhi is still where the user lives, and the name they gave later is kept too."""
    from mavis.domain.memory import ProfileUpdate
    from mavis.store.repo import profile as profile_repo

    async def no_summary(uid):
        return False

    monkeypatch.setattr(jobs, "maybe_summarize", no_summary)
    t1, t2 = clock.t - timedelta(hours=2), clock.t - timedelta(hours=1)

    def extraction(city, name):
        return Extraction(
            entities=[Entity(name=city, label="Place")],
            relations=[Relation(subject="User", rel="LOCATED_IN", object=city,
                                statement=f"Lives in {city}.")],
            profile_updates=[ProfileUpdate(field="name", value=name)])

    fake_llm.push_structured(extraction("Delhi", "Arjun"))  # turn 2, applied first
    await jobs.handle_learn(_job("learn:tg:b", user.id, {
        "text": "moved to Delhi, I'm Arjun", "source_ref": "tg:b", "trust": "user",
        "anchor_at": t2.isoformat()}))
    fake_llm.push_structured(extraction("Pune", "Jai"))  # turn 1's retry, 10 minutes later
    await jobs.handle_learn(_job("learn:tg:a:w1", user.id, {"text": "I live in Pune, call me Jai",
                                                          "source_ref": "tg:a", "trust": "user",
                                                          "anchor_at": t1.isoformat(), "retry": 1}))
    homes = {d["object"] for d in await memory.graph.dump(user.id) if d["relation"] == "LOCATED_IN"}
    assert homes == {"Delhi"}
    assert (await profile_repo.get(user.id)).name == "Arjun"


# --- a backlog of LEARN texts for one user is coalesced into one extraction ------------------------------


async def _park(user, n, text=None, trust="user", at=None, retry=1):
    payload = {"text": text or f"User: message number {n}", "source_ref": f"tg:q{n}", "trust": trust,
               "conversation": True, "retry": retry, "anchor_at": (at or timeutil.now()).isoformat()}
    await jobs.park_learn(user.id, payload, timeutil.now() + timedelta(minutes=3), f"r{retry}")


async def test_backlog_over_the_threshold_is_one_extraction_with_every_source_time(
        memory, user, fake_llm, monkeypatch, clock):
    from mavis.store.repo import events

    seen_texts = []
    real = memory.learn

    async def spy(uid, text, source_ref="", trust=None, **kw):
        seen_texts.append((text, source_ref, kw.get("anchor_at")))
        return await real(uid, text, source_ref, trust, **kw)

    async def no_summary(uid):
        return False

    monkeypatch.setattr(memory, "learn", spy)
    monkeypatch.setattr(jobs, "maybe_summarize", no_summary)
    base = clock.t - timedelta(hours=3)
    for n in range(jobs.LEARN_COALESCE_ABOVE + 2):
        await _park(user, n, at=base + timedelta(minutes=10 * n))
    fake_llm.push_structured(Extraction())
    current = {"text": "User: the newest message", "source_ref": "tg:new", "trust": "user",
               "conversation": True, "anchor_at": clock.t.isoformat()}
    await jobs.handle_learn(_job("learn:tg:new", user.id, current))
    assert len(seen_texts) == 1  # one extraction over everything
    text, ref, anchor = seen_texts[0]
    for n in range(jobs.LEARN_COALESCE_ABOVE + 2):
        assert f"message number {n}" in text
    assert "the newest message" in text
    assert text.index("message number 0") < text.index("message number 3") < text.index("the newest message")
    for n in range(jobs.LEARN_COALESCE_ABOVE + 2):  # each text's own time is in the batch, in order
        stamp = timeutil.to_local(base + timedelta(minutes=10 * n), user.timezone)
        assert f"[{stamp:%a %d %b %H:%M}]" in text
    assert anchor == clock.t  # the newest source time: facts are applied no earlier than the last statement
    assert await events.seen("learn:tg:new") and await events.seen("learn:tg:q0")
    assert await _learn_wakeups(user.id) == []  # the parked ones were consumed


async def test_a_short_queue_is_left_alone(memory, user, fake_llm, monkeypatch, clock):
    calls = []
    real = memory.learn

    async def spy(uid, text, *a, **kw):
        calls.append(text)
        return await real(uid, text, *a, **kw)

    async def no_summary(uid):
        return False

    monkeypatch.setattr(memory, "learn", spy)
    monkeypatch.setattr(jobs, "maybe_summarize", no_summary)
    for n in range(jobs.LEARN_COALESCE_ABOVE - 2):
        await _park(user, n)
    fake_llm.push_structured(Extraction())
    hi = {"text": "User: hi", "source_ref": "tg:x", "trust": "user"}
    await jobs.handle_learn(_job("learn:tg:x", user.id, hi))
    assert calls == ["User: hi"] and len(await _learn_wakeups(user.id)) == jobs.LEARN_COALESCE_ABOVE - 2


async def test_only_the_same_users_same_trust_texts_are_merged(memory, user, fake_llm, monkeypatch, clock):
    from mavis.store.repo import users as users_repo

    other, _ = await users_repo.get_or_create_by_chat(4242, "Other")
    calls = []

    async def spy(uid, text, *a, **kw):
        calls.append((uid, text))
        return Extraction()

    async def no_summary(uid):
        return False

    monkeypatch.setattr(memory, "learn", spy)
    monkeypatch.setattr(jobs, "maybe_summarize", no_summary)
    for n in range(jobs.LEARN_COALESCE_ABOVE + 1):
        await _park(user, n, trust="untrusted")  # another trust: never mixed into a trusted batch
        await _park(other, 100 + n)  # another user: never mixed
    await jobs.handle_learn(_job("learn:tg:y", user.id, {"text": "User: mine", "source_ref": "tg:y",
                                                       "trust": "user", "conversation": True}))
    assert calls == [(user.id, "User: mine")]


async def test_a_failed_batch_is_parked_again_whole(memory, user, monkeypatch, clock):
    async def busy(*a, **k):
        raise LLMError("timed out waiting for an LLM slot")

    monkeypatch.setattr(memory, "learn", busy)
    for n in range(jobs.LEARN_COALESCE_ABOVE + 1):
        await _park(user, n)
    await jobs.handle_learn(_job("learn:tg:z", user.id, {"text": "User: last", "source_ref": "tg:z",
                                                       "trust": "user", "conversation": True}))
    [w] = await _learn_wakeups(user.id)  # one wakeup carries the whole batch
    batch = w.payload["learn"]
    assert "message number 0" in batch["text"] and "last" in batch["text"]
    assert "tg:q0" in batch["merged_refs"] and "tg:z" in batch["merged_refs"]
