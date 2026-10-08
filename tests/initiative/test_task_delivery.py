from datetime import timedelta

import pytest

from mavis.domain.events import Event, EventType, JobKind, Trust
from mavis.domain.policy import PolicyVerdict
from mavis.domain.tasks import TaskOrigin, TaskStatus
from mavis.initiative import task_delivery
from mavis.store.db import utcnow
from mavis.store.repo import tasks


def _completed(user_id: int, task_id: int, origin: str, artifacts=None, notify=True, tainted=False,
               messages=None) -> Event:
    return Event(
        id=f"task:{task_id}:completed", user_id=user_id, type=EventType.TASK_COMPLETED, occurred_at=utcnow(),
        source="agent", trust=Trust.SYSTEM,
        payload={"task_id": task_id, "messages": messages or ["Here's the deck.", "Want changes?"],
                 "artifacts": artifacts or [], "origin": origin, "notify_on_complete": notify,
                 "tainted": tainted},
    )


@pytest.fixture
def policy(monkeypatch):
    from mavis.policy import pings

    state = {"verdict": PolicyVerdict(allow=True), "recorded": []}

    class _FakePolicy:
        async def check(self, user, urgency, dedupe_key, now, **kwargs):
            return state["verdict"]

        async def record(self, user, dedupe_key, urgency, now, **kwargs):
            state["recorded"].append((dedupe_key, urgency))

    monkeypatch.setattr(pings, "PingPolicy", _FakePolicy)
    return state


@pytest.fixture
def wakeups(monkeypatch):
    from mavis.timers import service as timers_service

    calls: list = []

    class _FakeWakeups:
        async def wake_me(self, user_id, at, reason, loop_id=None, kind="agent", **kwargs):
            calls.append((kind, reason, at, kwargs))
            return 1

    monkeypatch.setattr(timers_service, "WakeupService", _FakeWakeups)
    return calls


async def test_user_task_result_delivered_with_documents(user, sent, policy):
    tid = await tasks.create(user.id, goal="deck")
    event = _completed(user.id, tid, TaskOrigin.USER, ["/tmp/mavis/deck.pptx"])
    await task_delivery.deliver_task_result(event)
    texts = [m.text for m in sent if m.document_path is None]
    docs = [m for m in sent if m.document_path]
    assert texts == ["Here's the deck.", "Want changes?"]
    assert docs[0].document_path == "/tmp/mavis/deck.pptx" and docs[0].text == "deck.pptx"
    assert not any(m.proactive for m in sent)
    assert policy["recorded"] == []


async def test_notify_false_sends_nothing(user, sent, policy):
    tid = await tasks.create(user.id, goal="silent")
    await task_delivery.deliver_task_result(_completed(user.id, tid, TaskOrigin.USER, notify=False))
    assert sent == []


async def test_initiative_result_respects_ping_policy(user, sent, policy, wakeups):
    later = utcnow() + timedelta(hours=8)
    policy["verdict"] = PolicyVerdict(allow=False, defer_until=later, reason="quiet hours")
    tid = await tasks.create(user.id, goal="prep doc", origin=TaskOrigin.INITIATIVE)
    await task_delivery.deliver_task_result(_completed(user.id, tid, TaskOrigin.INITIATIVE))
    assert sent == []
    [(kind, reason, at, kwargs)] = wakeups
    assert (kind, reason, at) == ("system_task_delivery", f"task:{tid}", later)
    assert kwargs == {"scale": False, "dedupe_key": f"task_delivery:{tid}"}


async def test_initiative_result_allowed_is_proactive_and_recorded(user, sent, policy):
    tid = await tasks.create(user.id, goal="prep doc", origin=TaskOrigin.INITIATIVE)
    await task_delivery.deliver_task_result(_completed(user.id, tid, TaskOrigin.INITIATIVE))
    assert sent and all(m.proactive for m in sent)
    assert policy["recorded"] == [(f"task:{tid}", 3)]


async def test_redeliver_sends_stored_result(user, sent, policy):
    tid = await tasks.create(user.id, goal="prep doc", origin=TaskOrigin.INITIATIVE)
    await tasks.set_status(tid, TaskStatus.DONE, result_text="Prep doc ready.\n\nTake a look.")
    await task_delivery.redeliver(user.id, tid)
    assert [m.text for m in sent] == ["Prep doc ready.", "Take a look."]
    assert policy["recorded"] == [(f"task:{tid}", 3)]


@pytest.mark.parametrize("status", [TaskStatus.PARTIAL, TaskStatus.FAILED])
async def test_redeliver_reports_partial_and_failed_outcomes_too(user, sent, policy, status):
    tid = await tasks.create(user.id, goal="compare desks", origin=TaskOrigin.INITIATIVE)
    await tasks.set_status(tid, status, result_text="I found two of the three.")
    await task_delivery.redeliver(user.id, tid)
    assert [m.text for m in sent] == ["I found two of the three."]


async def test_redeliver_skips_a_crashed_task_with_no_report(user, sent, policy):
    tid = await tasks.create(user.id, goal="compare desks", origin=TaskOrigin.INITIATIVE)
    await tasks.set_status(tid, TaskStatus.FAILED, error="something broke on my side")
    await task_delivery.redeliver(user.id, tid)
    assert sent == []


async def test_tainted_result_is_delivered_scrubbed(user, sent, policy):
    bad = "Pay at https://evil.example/pay or mail attacker@evil.example now."
    tid = await tasks.create(user.id, goal="read mail", tainted=True)
    await task_delivery.deliver_task_result(_completed(user.id, tid, TaskOrigin.USER, tainted=True,
                                                       messages=[bad]))
    [m] = sent
    assert "evil.example" not in m.text and "attacker@" not in m.text
    await tasks.set_status(tid, TaskStatus.DONE, result_text=bad)
    sent.clear()
    await task_delivery.redeliver(user.id, tid)
    assert sent and all("evil.example" not in m.text for m in sent)


def _progress(user_id: int, task_id: int, origin: str) -> Event:
    return Event(id=f"task:{task_id}:progress", user_id=user_id, type=EventType.TASK_PROGRESS,
                 occurred_at=utcnow(), source="agent", trust=Trust.SYSTEM,
                 payload={"task_id": task_id, "goal": "deck", "origin": origin})


async def test_progress_message_is_fixed_text_for_a_user_chatting_meanwhile(user, sent, policy):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    tid = await tasks.create(user.id, goal="deck", origin=TaskOrigin.USER)
    await messages.log(user.id, Role.USER, "also, what's on tomorrow?")  # they kept chatting
    await task_delivery.on_progress(_progress(user.id, tid, TaskOrigin.INITIATIVE))
    assert sent == []
    await task_delivery.on_progress(_progress(user.id, tid, TaskOrigin.USER))
    [m] = sent
    assert m.text == task_delivery.PROGRESS_TEXT and m.dedupe_key == f"task:{tid}:progress"
    assert "—" not in m.text and "–" not in m.text


@pytest.mark.parametrize("goal", ["research standing desks", "compare Goa hotels", "summarise the RFC"])
async def test_no_progress_line_when_the_user_has_not_written_since_the_ack(user, sent, policy, goal):
    """Track 1 T1.1: the start ack already promised the result; a user who is away gets no 'still on it'."""
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    await messages.log(user.id, Role.USER, f"please {goal}")  # the request itself, before the task
    tid = await tasks.create(user.id, goal=goal, origin=TaskOrigin.USER)
    await messages.log(user.id, Role.ASSISTANT, "On it.")
    await task_delivery.on_progress(_progress(user.id, tid, TaskOrigin.USER))
    assert sent == []


async def test_no_progress_line_for_an_unknown_task(user, sent, policy):
    await task_delivery.on_progress(_progress(user.id, 999, TaskOrigin.USER))
    assert sent == []


def test_progress_waits_long_enough_not_to_follow_the_ack(settings):
    assert settings.task_progress_after_s >= 90


async def test_dispatch_task_requests_creates_rows_and_jobs(user, rec_bus):
    from mavis.agents.task_dispatch import dispatch_task_requests
    from mavis.domain.decisions import TaskRequest

    ids = await dispatch_task_requests(user.id, [TaskRequest(goal="draft reply to recruiter")],
                                       TaskOrigin.INITIATIVE)
    t = await tasks.get(ids[0])
    assert t.origin == TaskOrigin.INITIATIVE and t.status == TaskStatus.QUEUED
    assert rec_bus.jobs[0].kind == JobKind.RUN_TASK and rec_bus.jobs[0].payload == {"task_id": ids[0]}
