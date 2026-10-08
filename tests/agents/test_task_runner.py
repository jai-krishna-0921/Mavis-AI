import asyncio
from datetime import timedelta

import pytest

from mavis.agents import interrupts, orchestrator, task_dispatch
from mavis.agents import orchestrator_graph as og
from mavis.config import get_settings
from mavis.domain.decisions import ComposedMessage, TaskRequest
from mavis.domain.events import EventType, JobKind
from mavis.domain.plans import Plan, PlanStep
from mavis.domain.tasks import ApprovalStatus, StepOutcome, TaskOrigin, TaskStatus
from mavis.store.db import Session, utcnow
from mavis.store.models import PendingApproval
from mavis.store.repo import approvals, tasks
from mavis.tools.registry import current_task_id, get_registry

DASHES = ("—", "–")


@pytest.fixture
def wakeups(monkeypatch):
    from mavis.timers import service as timers_service

    class _Calls(list):
        extras: list[dict]

    calls = _Calls()
    extras: list[dict] = []

    class _FakeWakeups:
        async def wake_me(self, user_id, at, reason, loop_id=None, kind="agent", **kwargs):
            calls.append((kind, reason))
            extras.append(kwargs)
            return len(calls)

    monkeypatch.setattr(timers_service, "WakeupService", _FakeWakeups)
    calls.extras = extras
    return calls


def _one_step_plan() -> Plan:
    return Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="draft note")])


@pytest.fixture
def step_queues_note(monkeypatch):
    """The single plan step queues an approval for send_note('hi'), as a real tool call would."""

    async def _fake_step(step, user_id, context):
        await approvals.create(user_id, current_task_id.get(), "send_note", {"text": "hi"},
                               "Send note: hi", utcnow() + timedelta(hours=48))
        return StepOutcome(ok=True, text="drafted")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)


async def _start(user, fake_llm) -> int:
    fake_llm.push_structured(_one_step_plan())
    tid = await tasks.create(user.id, goal="send Jawahar a note")
    await orchestrator.run_task(tid)
    return tid


async def test_approval_interrupt_then_ok_executes(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, wakeups, step_queues_note
):
    tid = await _start(user, fake_llm)

    assert (await tasks.get(tid)).status == TaskStatus.AWAITING_APPROVAL
    assert note_tool == []
    prompt = sent[-1]
    pending = await approvals.next_open(tid)
    assert "Send note: hi" in prompt.text
    ids = [f"ap:{pending.id}:ok", f"ap:{pending.id}:edit", f"ap:{pending.id}:no"]
    assert [b.data for b in prompt.buttons[0]] == ids
    assert {k for k, _ in wakeups} == {"system_approval_remind", "system_approval_expire"}
    assert all(e["scale"] is False for e in wakeups.extras)
    assert {e["dedupe_key"] for e in wakeups.extras} == {
        f"approval:{pending.id}:remind", f"approval:{pending.id}:expire"}

    await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Sent it to Jawahar."]))
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "ok"})

    assert note_tool == ["hi"]
    assert (await approvals.get(pending.id)).status == ApprovalStatus.EXECUTED
    assert (await tasks.get(tid)).status == TaskStatus.DONE
    assert any(e.type == EventType.TASK_COMPLETED for e in rec_bus.events)


async def test_ok_is_at_most_once(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, wakeups, step_queues_note
):
    tid = await _start(user, fake_llm)
    pending = await approvals.next_open(tid)
    await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "ok"})
    assert note_tool == ["hi"]

    # a duplicate job: back to AWAITING_APPROVAL with the approval claimed again
    await tasks.set_status(tid, TaskStatus.AWAITING_APPROVAL)
    async with Session() as s:
        row = await s.get(PendingApproval, pending.id)
        row.status = ApprovalStatus.RESOLVING.value
        await s.commit()
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "ok"})
    assert note_tool == ["hi"]


async def test_edit_then_ok_uses_revised_args(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, wakeups, step_queues_note
):
    tid = await _start(user, fake_llm)
    pending = await approvals.next_open(tid)

    await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    note_args = get_registry().get("send_note").args_model
    fake_llm.push_structured(note_args(text="Hello Jawahar, hope you're well."))
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "edit",
                                         "instructions": "make it more formal"})

    assert note_tool == []
    assert "Hello Jawahar, hope you're well." in sent[-1].text
    assert len(wakeups) == 2  # expiry wakeups scheduled once, not per re-prompt

    await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Sent."]))
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "ok"})
    assert note_tool == ["Hello Jawahar, hope you're well."]


async def test_cancel_rejects_without_running(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, wakeups, step_queues_note
):
    tid = await _start(user, fake_llm)
    pending = await approvals.next_open(tid)
    await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Okay, not sending it."]))
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "no"})
    assert note_tool == []
    assert (await approvals.get(pending.id)).status == ApprovalStatus.REJECTED
    assert (await tasks.get(tid)).status == TaskStatus.DONE


async def test_resume_ignored_unless_awaiting_approval(user, memory_checkpointer):
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.resume_task(tid, {"approval_id": 1, "decision": "ok"})
    assert (await tasks.get(tid)).status == TaskStatus.QUEUED


async def test_concurrency_limit_leaves_task_queued(user, memory_checkpointer, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_max_concurrency", 1)
    busy = await tasks.create(user.id, goal="busy")
    await tasks.claim(busy, TaskStatus.QUEUED, TaskStatus.RUNNING)
    tid = await tasks.create(user.id, goal="waits")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.QUEUED


async def test_finished_task_kicks_next_queued(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    async def _fake_step(step, user_id, context):
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(_one_step_plan())
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    first = await tasks.create(user.id, goal="a")
    second = await tasks.create(user.id, goal="b")
    await orchestrator.run_task(first)
    runs = [j for j in rec_bus.jobs if j.kind == JobKind.RUN_TASK]
    assert [j.payload["task_id"] for j in runs] == [second]


async def test_timeout_marks_failed_and_tells_user(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(get_settings(), "task_timeout_s", 0.05)

    async def _slow_step(step, user_id, context):
        await asyncio.sleep(1)
        return StepOutcome(ok=True, text="late")

    monkeypatch.setattr(og, "run_step_agent", _slow_step)
    fake_llm.push_structured(_one_step_plan())
    tid = await tasks.create(user.id, goal="slow thing")
    await orchestrator.run_task(tid)
    task = await tasks.get(tid)
    assert task.status == TaskStatus.FAILED and task.finished_at is not None
    assert "longer than" in sent[-1].text
    assert not any(d in sent[-1].text for d in DASHES)


async def test_timeout_does_not_overwrite_a_cancel(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    # The cancel must land before the wall clock runs out (graph compile, checkpointer and planner take
    # time on a loaded machine): the step cancels, then outlives a timeout that cannot beat it there.
    monkeypatch.setattr(get_settings(), "task_timeout_s", 1.0)
    tid = await tasks.create(user.id, goal="slow thing")
    cancelled = asyncio.Event()

    async def _slow_step(step, user_id, context):
        await tasks.cancel(user_id, tid)
        cancelled.set()
        await asyncio.sleep(5)
        return StepOutcome(ok=True, text="late")

    monkeypatch.setattr(og, "run_step_agent", _slow_step)
    fake_llm.push_structured(_one_step_plan())
    await orchestrator.run_task(tid)
    assert cancelled.is_set()  # the ordering this test is about: cancel first, timeout after
    assert (await tasks.get(tid)).status == TaskStatus.CANCELLED
    assert sent == []


async def test_crash_marks_failed(user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("secret-token-123")

    monkeypatch.setattr(og, "make_plan", _boom)
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    task = await tasks.get(tid)
    assert task.status == TaskStatus.FAILED
    assert "secret-token-123" not in (task.error or "") + sent[-1].text


async def test_progress_event_after_threshold(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    monkeypatch.setattr(get_settings(), "task_progress_after_s", 0.01)

    async def _step(step, user_id, context):
        await asyncio.sleep(0.1)
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_one_step_plan())
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    tid = await tasks.create(user.id, goal="g", origin=TaskOrigin.INITIATIVE)
    await orchestrator.run_task(tid)
    progress = [
        e for e in rec_bus.events
        if e.type == EventType.TASK_PROGRESS and e.payload["task_id"] == tid
    ]
    assert len(progress) == 1 and progress[0].payload["origin"] == "initiative"


async def test_no_progress_event_for_a_quick_task(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    async def _step(step, user_id, context):
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_one_step_plan())
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    assert not any(e.type == EventType.TASK_PROGRESS for e in rec_bus.events)


async def test_tainted_task_runs_steps_tainted(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    seen: list[bool] = []

    async def _step(step, user_id, context):
        seen.append(og.step_tainted.get())
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_one_step_plan())
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    tid = await tasks.create(user.id, goal="g", tainted=True)
    await orchestrator.run_task(tid)
    assert seen == [True]


async def test_background_priority_for_every_llm_call(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    async def _step(step, user_id, context):
        return StepOutcome(ok=True, text="x")

    seen: list[str] = []
    real = og.llm.structured

    async def _spy(schema, system, user_msg, tier=None, priority="interactive", fallback=None):
        seen.append(priority)
        return await real(schema, system, user_msg, tier=tier, priority=priority, fallback=fallback)

    monkeypatch.setattr(og.llm, "structured", _spy)
    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_one_step_plan())
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    assert seen and set(seen) == {"background"}


async def test_cancel_after_approval_ran_still_tells_the_user(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, wakeups, step_queues_note, monkeypatch
):
    tid = await _start(user, fake_llm)
    pending = await approvals.next_open(tid)
    await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)

    real = get_registry().execute_approved

    async def _run_then_cancel(approval_id):
        out = await real(approval_id)
        await tasks.cancel(user.id, tid)  # the user cancels while the action is running
        return out

    monkeypatch.setattr(get_registry(), "execute_approved", _run_then_cancel)
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "ok"})

    assert note_tool == ["hi"]
    assert (await tasks.get(tid)).status == TaskStatus.CANCELLED
    assert (await approvals.get(pending.id)).status == ApprovalStatus.EXECUTED
    assert "went through" in sent[-1].text and "Send note: hi" in sent[-1].text
    assert not any(d in sent[-1].text for d in DASHES)
    assert not any(e.type == EventType.TASK_COMPLETED for e in rec_bus.events)


async def test_cancel_while_running_suppresses_the_prompt(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, wakeups, monkeypatch
):
    tid = await tasks.create(user.id, goal="g")

    async def _step(step, user_id, context):
        await approvals.create(user_id, current_task_id.get(), "send_note", {"text": "hi"},
                               "Send note: hi", utcnow() + timedelta(hours=48))
        await tasks.cancel(user_id, tid)
        return StepOutcome(ok=True, text="drafted")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_one_step_plan())
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.CANCELLED
    assert not any("Send note" in m.text for m in sent)


async def test_dispatch_task_requests_creates_and_enqueues(user, rec_bus):
    ids = await task_dispatch.dispatch_task_requests(
        user.id,
        [TaskRequest(goal="a", context="c"), TaskRequest(goal="b", notify_on_complete=False)],
        TaskOrigin.INITIATIVE,
    )
    assert len(ids) == 2
    assert [j.payload["task_id"] for j in rec_bus.jobs if j.kind == JobKind.RUN_TASK] == ids
    assert (await tasks.get(ids[1])).notify_on_complete is False
    assert (await tasks.get(ids[0])).origin == "initiative"


async def test_enqueue_run_uses_an_explicit_bus(user, rec_bus):
    from tests.conftest import RecordingBus

    other = RecordingBus()
    await task_dispatch.enqueue_run(7, user.id, bus=other)
    assert [j.payload["task_id"] for j in other.jobs] == [7] and rec_bus.jobs == []


async def test_interrupt_registry_dispatches_by_type(monkeypatch):
    got: list[tuple] = []

    async def _h(task_id, user_id, payload):
        got.append((task_id, user_id, payload["type"]))

    monkeypatch.setattr(interrupts, "INTERRUPT_HANDLERS", dict(interrupts.INTERRUPT_HANDLERS))
    interrupts.register_interrupt_handler("custom", _h)
    assert await interrupts.dispatch_interrupt(1, 2, {"type": "custom"}) is True
    assert got == [(1, 2, "custom")]
    assert await interrupts.dispatch_interrupt(1, 2, {"type": "nope"}) is False
    assert {"approval", "connect"} <= set(interrupts.INTERRUPT_HANDLERS)


async def test_default_connect_handler_says_so_and_resumes_as_declined(user, rec_bus, sent):
    assert await interrupts.dispatch_interrupt(5, user.id, {"type": "connect", "capability": "gmail"})
    assert "gmail" in sent[-1].text and not any(d in sent[-1].text for d in DASHES)
    job = rec_bus.jobs[-1]
    assert job.kind == JobKind.RESUME_TASK
    value = {"type": "connect", "capability": "gmail", "connected": False}
    assert job.payload == {"task_id": 5, "value": value}


async def test_failed_task_leaves_no_open_approvals(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, wakeups, monkeypatch
):
    monkeypatch.setattr(get_settings(), "task_timeout_s", 0.1)
    tid = await tasks.create(user.id, goal="g")
    ids: list[int] = []

    async def _step(step, user_id, context):
        for _ in range(3):
            ids.append(await approvals.create(user_id, tid, "send_note", {"text": "hi"},
                                              "Send note: hi", utcnow() + timedelta(hours=48)))
        await approvals.claim(ids[1], {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
        await approvals.claim(ids[2], {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
        await approvals.claim(ids[2], {ApprovalStatus.RESOLVING}, ApprovalStatus.EXECUTED)
        await approvals.mark_started(ids[2])  # may have run
        await asyncio.sleep(1)
        return StepOutcome(ok=True, text="late")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_one_step_plan())
    await orchestrator.run_task(tid)

    assert (await tasks.get(tid)).status == TaskStatus.FAILED
    assert (await approvals.get(ids[0])).status == ApprovalStatus.REJECTED
    assert (await approvals.get(ids[1])).status == ApprovalStatus.FAILED
    assert (await approvals.get(ids[2])).status == ApprovalStatus.EXECUTED  # left for the sweep
    assert await approvals.next_open(tid) is None
    assert any("may have gone through" in m.text and "Send note: hi" in m.text for m in sent)
    assert not any(d in m.text for m in sent for d in DASHES)


async def test_unchanged_preview_still_reprompts(user, sent, wakeups):
    tid = await tasks.create(user.id, goal="g")
    aid = await approvals.create(user.id, tid, "send_note", {"text": "hi"}, "Send note: hi",
                                 utcnow() + timedelta(hours=48))
    from mavis.policy import approvals as flow

    await flow.send_approval_prompt(user.id, {"approval_id": aid})
    await asyncio.sleep(0.01)
    await flow.send_approval_prompt(user.id, {"approval_id": aid})
    prompts = [m for m in sent if m.buttons]
    assert len(prompts) == 2
    assert prompts[0].dedupe_key != prompts[1].dedupe_key
    assert len(wakeups) == 2  # expiry wakeups still scheduled once


async def test_stale_running_task_is_failed_and_stops_blocking(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    from sqlalchemy import update

    from mavis.store.models import Task

    async def _step(step, user_id, context):
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _step)
    stuck = await tasks.create(user.id, goal="stuck")
    await tasks.claim(stuck, TaskStatus.QUEUED, TaskStatus.RUNNING)
    async with Session() as s:
        await s.execute(update(Task).where(Task.id == stuck).values(
            started_at=utcnow() - timedelta(seconds=get_settings().task_timeout_s + 3600)))
        await s.commit()
    fake_llm.push_structured(_one_step_plan())
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    tid = await tasks.create(user.id, goal="next")
    await orchestrator.run_task(tid)
    assert (await tasks.get(stuck)).status == TaskStatus.FAILED
    assert (await tasks.get(tid)).status == TaskStatus.DONE


async def test_concurrent_run_jobs_start_only_one(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    started: list[int] = []

    async def _step(step, user_id, context):
        started.append(step.id)
        await asyncio.sleep(0.1)
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _step)
    fake_llm.push_structured(_one_step_plan())
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    a = await tasks.create(user.id, goal="a")
    b = await tasks.create(user.id, goal="b")
    await asyncio.gather(orchestrator.run_task(a), orchestrator.run_task(b))
    assert len(started) == 1
    assert {(await tasks.get(a)).status, (await tasks.get(b)).status} == {TaskStatus.DONE, TaskStatus.QUEUED}


async def test_inner_timeout_is_not_reported_as_the_task_limit(
    user, fake_llm, rec_bus, sent, memory_checkpointer, monkeypatch
):
    async def _plan(*a, **k):
        raise TimeoutError("http")

    monkeypatch.setattr(og, "make_plan", _plan)
    tid = await tasks.create(user.id, goal="g")
    await orchestrator.run_task(tid)
    assert (await tasks.get(tid)).status == TaskStatus.FAILED
    assert "longer than I allow" not in sent[-1].text
    assert "timed out" in sent[-1].text


async def test_restart_mid_task_fails_it_closes_approvals_and_kicks_the_queue(user, rec_bus, sent):
    """I1: a restart left A RUNNING (no job) and B QUEUED behind it (deferred, no job). The worker start
    hook fails A through claim, closes its approvals, tells the user, and re-enqueues B."""
    from mavis.domain.tasks import TaskKind

    a = await tasks.create(user.id, goal="research")
    await tasks.claim(a, TaskStatus.QUEUED, TaskStatus.RUNNING)
    aid = await approvals.create(user.id, a, "send_note", {"text": "hi"}, "Send note: hi",
                                 utcnow() + timedelta(hours=48))
    b = await tasks.create(user.id, goal="send", kind=TaskKind.APPROVAL)
    await orchestrator.recover_tasks_on_start()
    task_a = await tasks.get(a)
    assert task_a.status == TaskStatus.FAILED and "restarted" in task_a.error
    assert (await approvals.get(aid)).status not in (ApprovalStatus.PENDING, ApprovalStatus.RESOLVING)
    assert any(m.dedupe_key == f"task:{a}:failed" for m in sent)
    runs = [j for j in rec_bus.jobs if j.kind == JobKind.RUN_TASK]
    assert [j.payload["task_id"] for j in runs] == [b]
    # a second sweep is harmless: nothing more is failed, and only a duplicate RUN_TASK (claimed once)
    assert await orchestrator.recover_tasks() == 0


async def test_periodic_sweep_fails_only_tasks_past_the_wall_clock(user, rec_bus, sent):
    from sqlalchemy import update

    from mavis.store.models import Task

    fresh = await tasks.create(user.id, goal="fresh")
    await tasks.claim(fresh, TaskStatus.QUEUED, TaskStatus.RUNNING)
    old = await tasks.create(user.id, goal="old")
    await tasks.claim(old, TaskStatus.QUEUED, TaskStatus.RUNNING)
    async with Session() as s:
        await s.execute(update(Task).where(Task.id == old).values(
            started_at=utcnow() - timedelta(seconds=get_settings().task_timeout_s + 120)))
        await s.commit()
    assert await orchestrator.recover_tasks(user.id) == 1
    assert (await tasks.get(old)).status == TaskStatus.FAILED
    assert (await tasks.get(fresh)).status == TaskStatus.RUNNING


async def test_approval_task_does_not_wait_for_the_task_slot(user, rec_bus, sent, monkeypatch):
    """I2: a chat approval prompt appears at once even while a research task holds the one slot."""
    from mavis.domain.tasks import TaskKind

    driven: list[int] = []

    async def fake_drive(task_id, user_id, graph_input):
        driven.append(task_id)

    monkeypatch.setattr(orchestrator, "_drive", fake_drive)
    research = await tasks.create(user.id, goal="research laptops")
    await tasks.claim(research, TaskStatus.QUEUED, TaskStatus.RUNNING)
    other = await tasks.create(user.id, goal="another research")
    await orchestrator.run_task(other)
    assert driven == [] and (await tasks.get(other)).status == TaskStatus.QUEUED  # still slot-bound
    ap_task = await tasks.create(user.id, goal="send email", kind=TaskKind.APPROVAL)
    await orchestrator.run_task(ap_task)
    assert driven == [ap_task] and (await tasks.get(ap_task)).status == TaskStatus.RUNNING
    # a running approval task takes no slot either
    await tasks.claim(research, TaskStatus.RUNNING, TaskStatus.DONE)
    await orchestrator.run_task(other)
    assert driven == [ap_task, other]


async def test_no_progress_line_for_an_approval_task(user, rec_bus):
    """M6: a slow approval edit must not send "still on it"."""
    from mavis.domain.tasks import TaskKind

    tid = await tasks.create(user.id, goal="send email", kind=TaskKind.APPROVAL)
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.RUNNING)
    await orchestrator._progress_after(tid, user.id, 0)
    assert not [e for e in rec_bus.events if e.type == EventType.TASK_PROGRESS]
