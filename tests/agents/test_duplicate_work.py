"""Hotfix 3 RC2: one chat request must yield one task and one approval card.

Prod: one interview-invite request produced six initiative tasks (LOOP_CREATED of the same turn) and
four pending calendar approvals (chat follow-ups re-queued it while two tasks queued it too)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mavis.agents import orchestrator, task_dispatch
from mavis.domain.decisions import (
    ComposedMessage,
    InitiativeDecision,
    NotifyIntent,
    TaskRequest,
    WakeupRequest,
)
from mavis.domain.events import Event, EventType, JobKind
from mavis.domain.tasks import ApprovalStatus, TaskOrigin, TaskStatus
from mavis.initiative.handler import _quiet_after_turn
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks
from mavis.tools import chat_tools
from mavis.tools.registry import current_task_id

GOAL = "Send an interview invite to Jane for Friday 3pm"


def _loop_created(source: str) -> Event:
    return Event(id="loop:7:created", user_id=1, type=EventType.LOOP_CREATED,
                 occurred_at=datetime.now(UTC), source="loops",
                 payload={"id": 7, "title": "Interview Jane", "source": source})


def _decision() -> InitiativeDecision:
    return InitiativeDecision(
        act=[TaskRequest(goal=GOAL)],
        wakeups=[WakeupRequest(at=datetime.now(UTC) + timedelta(hours=1), reason="check invite")],
        notify=NotifyIntent(urgency=2, intent="tell them about the invite"),
    )


# --- initiative: the chat turn owns actions on loops it just created ------------------------------


def test_quiet_after_turn_drops_acts_for_loops_from_chat() -> None:
    out = _quiet_after_turn(_loop_created("tg:update:412982316"), _decision())
    assert out.act == [] and out.notify is None
    assert len(out.wakeups) == 1  # follow-through stays


def test_quiet_after_turn_drops_acts_even_without_a_notify() -> None:
    d = _decision().model_copy(update={"notify": None})
    assert _quiet_after_turn(_loop_created("tg:update:1"), d).act == []


def test_quiet_after_turn_keeps_acts_for_other_sources() -> None:
    d = _decision()
    assert _quiet_after_turn(_loop_created("gmail:abc"), d).act == d.act


# --- task dedupe -----------------------------------------------------------------------------------


async def test_dispatch_skips_a_near_identical_active_task(user, rec_bus) -> None:
    [first] = await task_dispatch.dispatch_task_requests(user.id, [TaskRequest(goal=GOAL)],
                                                         TaskOrigin.INITIATIVE)
    [again] = await task_dispatch.dispatch_task_requests(
        user.id, [TaskRequest(goal="send interview invite to Jane for Friday 3pm")], TaskOrigin.INITIATIVE)
    assert again == first
    assert len([j for j in rec_bus.jobs if j.kind == JobKind.RUN_TASK]) == 1


async def test_dispatch_creates_a_new_task_once_the_old_one_finished(user, rec_bus) -> None:
    [first] = await task_dispatch.dispatch_task_requests(user.id, [TaskRequest(goal=GOAL)], TaskOrigin.USER)
    await tasks.set_status(first, TaskStatus.DONE)
    [second] = await task_dispatch.dispatch_task_requests(user.id, [TaskRequest(goal=GOAL)], TaskOrigin.USER)
    assert second != first


async def test_dispatch_keeps_different_goals_apart(user, rec_bus) -> None:
    ids = await task_dispatch.dispatch_task_requests(
        user.id, [TaskRequest(goal="research flights to Goa"), TaskRequest(goal="research hotels in Goa")],
        TaskOrigin.USER)
    assert len(set(ids)) == 2


async def test_start_task_refers_to_the_existing_task(user, rec_bus) -> None:
    [first] = await task_dispatch.dispatch_task_requests(user.id, [TaskRequest(goal=GOAL)],
                                                         TaskOrigin.INITIATIVE)
    out = await chat_tools.start_task(user.id, chat_tools.StartTaskArgs(goal=GOAL))
    assert f"#{first}" in out and "already" in out.lower()
    assert len([j for j in rec_bus.jobs if j.kind == JobKind.RUN_TASK]) == 1


# --- approval dedupe -------------------------------------------------------------------------------


def _invite(summary: str = "Interview: Jane", description: str = "Looking forward") -> dict:
    return {"summary": summary, "start": "2026-10-09T15:00:00+05:30", "duration_minutes": 30,
            "attendees": ["jane@example.com"], "description": description}


def test_equivalent_args_ignore_free_text_and_formatting() -> None:
    a = _invite()
    b = _invite(summary="  interview:   JANE ", description="See you then!")
    b["start"] = "2026-10-09T09:30:00Z"  # same instant
    assert approvals.equivalent(a, b)
    c = _invite()
    c["attendees"] = ["bob@example.com"]
    assert not approvals.equivalent(a, c)
    d = _invite()
    d["start"] = "2026-10-10T15:00:00+05:30"
    assert not approvals.equivalent(a, d)


def test_text_only_args_compare_in_full() -> None:
    assert not approvals.equivalent({"text": "hi"}, {"text": "bye"})
    assert approvals.equivalent({"text": "hi"}, {"text": "hi"})


async def test_queue_reuses_an_equivalent_pending_approval_from_another_task(user, note_tool) -> None:
    from mavis.tools.registry import get_registry

    other = await tasks.create(user.id, goal="earlier task")
    existing = await approvals.create(user.id, other, "send_note", {"text": "hi"}, "Send note: hi",
                                      utcnow() + timedelta(hours=48))
    [lc] = [t for t in get_registry().for_agent("conversation", user.id) if t.name == "send_note"]
    out = await lc.ainvoke({"text": "hi"})
    assert out.startswith(f"ALREADY_AWAITING_APPROVAL #{existing}")
    assert [a.id for a in await approvals.open_for_user(user.id)] == [existing]


async def test_queue_still_creates_a_different_approval(user, note_tool) -> None:
    from mavis.tools.registry import get_registry

    other = await tasks.create(user.id, goal="earlier task")
    await approvals.create(user.id, other, "send_note", {"text": "hi"}, "Send note: hi",
                           utcnow() + timedelta(hours=48))
    [lc] = [t for t in get_registry().for_agent("conversation", user.id) if t.name == "send_note"]
    assert (await lc.ainvoke({"text": "something else"})).startswith("QUEUED_FOR_APPROVAL #")
    assert len(await approvals.open_for_user(user.id)) == 2


# --- siblings are superseded once one is decided ---------------------------------------------------


async def _sibling(user_id: int, status: TaskStatus) -> tuple[int, int]:
    tid = await tasks.create(user_id, goal="duplicate task")
    await tasks.set_status(tid, status)
    aid = await approvals.create(user_id, tid, "send_note", {"text": "hi"}, "Send note: hi",
                                 utcnow() + timedelta(hours=48))
    return tid, aid


async def test_executing_an_approval_supersedes_its_pending_duplicates(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, monkeypatch
) -> None:
    from mavis.agents import orchestrator_graph as og
    from mavis.domain.plans import Plan, PlanStep
    from mavis.domain.tasks import StepOutcome
    from mavis.timers import service as timers_service

    class _NoWakeups:
        async def wake_me(self, *a, **k):
            return 1

    monkeypatch.setattr(timers_service, "WakeupService", _NoWakeups)

    async def _fake_step(step, user_id, context):
        await approvals.create(user_id, current_task_id.get(), "send_note", {"text": "hi"},
                               "Send note: hi", utcnow() + timedelta(hours=48))
        return StepOutcome(ok=True, text="drafted")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    waiting_tid, waiting_dup = await _sibling(user.id, TaskStatus.AWAITING_APPROVAL)
    busy_tid, busy_dup = await _sibling(user.id, TaskStatus.QUEUED)

    fake_llm.push_structured(Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="x")]))
    tid = await tasks.create(user.id, goal="send Jawahar a note")
    await orchestrator.run_task(tid)
    pending = await approvals.next_open(tid)
    await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Sent."]))
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "ok"})

    assert note_tool == ["hi"]
    # the waiting task is resumed with a "superseded" decision so it does not hang on a dead card
    assert (await approvals.get(waiting_dup)).status == ApprovalStatus.RESOLVING
    [job] = [j for j in rec_bus.jobs if j.kind == JobKind.RESUME_TASK]
    assert job.payload == {"task_id": waiting_tid, "approval_id": waiting_dup, "decision": "superseded",
                           "instructions": ""}
    # a task that has not reached its gate yet just finds the row closed
    busy = await approvals.get(busy_dup)
    assert busy.status == ApprovalStatus.REJECTED and "superseded" in (busy.result or "")


async def test_superseded_decision_closes_the_approval_without_running_it(
    user, fake_llm, rec_bus, sent, note_tool, memory_checkpointer, monkeypatch
) -> None:
    from mavis.agents import orchestrator_graph as og
    from mavis.domain.plans import Plan, PlanStep
    from mavis.domain.tasks import StepOutcome
    from mavis.timers import service as timers_service

    class _NoWakeups:
        async def wake_me(self, *a, **k):
            return 1

    monkeypatch.setattr(timers_service, "WakeupService", _NoWakeups)

    async def _fake_step(step, user_id, context):
        await approvals.create(user_id, current_task_id.get(), "send_note", {"text": "hi"},
                               "Send note: hi", utcnow() + timedelta(hours=48))
        return StepOutcome(ok=True, text="drafted")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="x")]))
    tid = await tasks.create(user.id, goal="send Jawahar a note")
    await orchestrator.run_task(tid)
    pending = await approvals.next_open(tid)
    await approvals.claim(pending.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Already done earlier."]))
    await orchestrator.resume_task(tid, {"approval_id": pending.id, "decision": "superseded"})
    assert note_tool == []
    row = await approvals.get(pending.id)
    assert row.status == ApprovalStatus.REJECTED and "superseded" in (row.result or "")
    assert (await tasks.get(tid)).status == TaskStatus.DONE
