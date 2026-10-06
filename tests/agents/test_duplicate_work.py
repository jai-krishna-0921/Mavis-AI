"""Hotfix 3 RC2: one chat request must yield one task and one approval card.

Prod: one interview-invite request produced six initiative tasks (LOOP_CREATED of the same turn) and
four pending calendar approvals (chat follow-ups re-queued it while two tasks queued it too)."""

from __future__ import annotations

from datetime import timedelta

from mavis.agents import orchestrator, task_dispatch
from mavis.domain import timeutil
from mavis.domain.decisions import (
    ComposedMessage,
    InitiativeDecision,
    NotifyIntent,
    TaskRequest,
    WakeupRequest,
)
from mavis.domain.events import Event, EventType, JobKind
from mavis.domain.loops import LoopOrigin
from mavis.domain.tasks import ApprovalStatus, TaskOrigin, TaskStatus
from mavis.initiative.handler import _quiet_after_turn
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks
from mavis.tools import chat_tools
from mavis.tools.registry import current_task_id

GOAL = "Send an interview invite to Jane for Friday 3pm"


def _loop_created(origin: LoopOrigin) -> Event:
    return Event(id="loop:7:created", user_id=1, type=EventType.LOOP_CREATED,
                 occurred_at=timeutil.now(), source="loops",
                 payload={"id": 7, "title": "Interview Jane", "source": "x", "origin": origin.value})


def _decision() -> InitiativeDecision:
    return InitiativeDecision(
        act=[TaskRequest(goal=GOAL)],
        wakeups=[WakeupRequest(at=timeutil.now() + timedelta(hours=1), reason="check invite")],
        notify=NotifyIntent(urgency=2, intent="tell them about the invite"),
    )


# --- initiative: the chat turn owns actions on loops it just created ------------------------------


def test_quiet_after_turn_drops_acts_for_loops_from_chat() -> None:
    out = _quiet_after_turn(_loop_created(LoopOrigin.CONVERSATION), _decision())
    assert out.act == [] and out.notify is None
    assert len(out.wakeups) == 1  # follow-through stays


def test_quiet_after_turn_drops_acts_even_without_a_notify() -> None:
    d = _decision().model_copy(update={"notify": None})
    assert _quiet_after_turn(_loop_created(LoopOrigin.CONVERSATION), d).act == []


def test_quiet_after_turn_keeps_acts_for_other_sources() -> None:
    d = _decision()
    assert _quiet_after_turn(_loop_created(LoopOrigin.REASONER), d).act == d.act


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


def _identity(tool: str) -> tuple[str, ...]:
    """The identity a real tool declares (phase A: declared on the tool, not listed here)."""
    from mavis.tools.registry import get_registry

    return get_registry().get(tool).identity


def test_equivalent_args_ignore_free_text_and_formatting() -> None:
    ident = _identity("calendar_create_event")
    a = _invite()
    b = _invite(summary="  interview:   JANE ", description="See you then!")
    b["start"] = "2026-10-09T09:30:00Z"  # same instant
    assert approvals.equivalent(a, b, ident)
    c = _invite()
    c["attendees"] = ["bob@example.com"]
    assert not approvals.equivalent(a, c, ident)
    d = _invite()
    d["start"] = "2026-10-10T15:00:00+05:30"
    assert not approvals.equivalent(a, d, ident)


def test_text_only_args_compare_in_full() -> None:
    assert not approvals.equivalent({"text": "hi"}, {"text": "bye"})
    assert approvals.equivalent({"text": "hi"}, {"text": " HI "})


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


# --- review round 2 (I1): free text is ignored only where key fields identify the action ------------


def test_slack_messages_with_different_text_are_different_actions() -> None:
    a = {"channel": "#team", "text": "Standup moved to 11"}
    b = {"channel": "#team", "text": "Lunch is on me today"}
    ident = _identity("slack_send")
    assert not approvals.equivalent(a, b, ident)
    assert approvals.equivalent(a, {"channel": "#team", "text": "  standup moved to 11 "}, ident)


def test_replies_with_different_bodies_are_different_actions() -> None:
    a = {"thread_id": "t1", "to": "raj@example.com", "body": "Yes, Monday works."}
    b = {"thread_id": "t1", "to": "raj@example.com", "body": "Sorry, I can't make it."}
    assert not approvals.equivalent(a, b, _identity("mail_reply"))
    page = {"parent_id": "p1", "title": "Notes"}
    assert not approvals.equivalent({**page, "content": "a"}, {**page, "content": "b"},
                                    _identity("notion_create_page"))


def test_calendar_twin_with_a_reworded_description_is_one_action() -> None:
    assert approvals.equivalent(_invite(description="Looking forward"),
                                _invite(description="Excited to meet you!"),
                                _identity("calendar_create_event"))
    # phase A fix round 1: the body is what the email says, so a reworded body is another message
    # (with the same target, it corrects the waiting card instead: tests/policy/test_approval_identity.py)
    mail = {"to": ["raj@example.com"], "subject": "Interview", "cc": []}
    assert not approvals.equivalent({**mail, "body": "Hi Raj"}, {**mail, "body": "Hello Raj,"},
                                    _identity("mail_send"))
    assert approvals.equivalent({**mail, "body": "Hi Raj"}, {**mail, "body": " hi  raj"},
                                _identity("mail_send"))


async def test_queue_keeps_two_slack_messages_to_one_channel(user, fresh_registry) -> None:
    from pydantic import BaseModel

    from mavis.domain.policy import RiskClass
    from mavis.tools.registry import MavisTool

    class SlackArgs(BaseModel):
        channel: str
        text: str

    async def _send(user_id, args):
        return "sent"

    fresh_registry.register(MavisTool("slack_send", "Post a Slack message.", SlackArgs, RiskClass.OUTWARD,
                                      _send, agents=frozenset({"conversation"})))
    other = await tasks.create(user.id, goal="earlier task")
    await approvals.create(user.id, other, "slack_send", {"channel": "#team", "text": "Standup moved"},
                           "Slack", utcnow() + timedelta(hours=48))
    [lc] = fresh_registry.for_agent("conversation", user.id)
    out = await lc.ainvoke({"channel": "#team", "text": "Lunch is on me"})
    assert out.startswith("QUEUED_FOR_APPROVAL #")
    assert len(await approvals.open_for_user(user.id)) == 2


# --- review round 2 (I2): a task-specific goal rule (days, times and numbers count) ----------------
import pytest  # noqa: E402

from mavis.store.repo.tasks import same_goal  # noqa: E402

INCIDENT_A = ("Create a calendar event for today at 3 PM IST and send an interview invite to "
              "jai261003@gmail.com")
INCIDENT_B = ("Create a calendar event for the 3 PM IST interview and send the invite to "
              "jai261003@gmail.com")


@pytest.mark.parametrize("a,b", [
    ("Schedule interview with Raj on Monday at 10am", "Schedule interview with Raj on Tuesday at 3pm"),
    ("Book flights to Paris", "Book flights and hotels to Paris"),
    ("Compare the best laptops under 1000", "Compare the best laptops under 1500"),
    ("Draft the weekly report for the marketing team at 3pm",
     "Draft the weekly report for the marketing team at 4pm"),
])
def test_different_goals_are_not_duplicates(a, b) -> None:
    assert not same_goal(a, b)


def test_identical_and_reworded_incident_goals_are_duplicates() -> None:
    assert same_goal(GOAL, GOAL) and same_goal(GOAL, "  send an interview invite to jane for friday 3pm!")
    assert same_goal(INCIDENT_A, INCIDENT_B)  # token Jaccard exactly 0.8, same numbers: one task


async def test_dispatch_starts_monday_and_tuesday_interviews_separately(user, rec_bus) -> None:
    ids = await task_dispatch.dispatch_task_requests(user.id, [
        TaskRequest(goal="Schedule interview with Raj on Monday at 10am"),
        TaskRequest(goal="Schedule interview with Raj on Tuesday at 3pm"),
    ], TaskOrigin.USER)
    assert len(set(ids)) == 2


# --- review round 3: approval dedupe is taint-aware -------------------------------------------------
MAIL = {"to": ["raj@example.com"], "subject": "Interview", "cc": []}


def _mail_tool(fresh_registry):
    from pydantic import BaseModel

    from mavis.domain.policy import RiskClass
    from mavis.tools.registry import MavisTool

    class MailArgs(BaseModel):
        to: list[str]
        subject: str
        body: str
        cc: list[str] = []

    async def _send(user_id, args):
        return "sent"

    fresh_registry.register(MavisTool("mail_send", "Send an email.", MailArgs, RiskClass.OUTWARD, _send,
                                      agents=frozenset({"conversation"}), identity=("to", "subject")))


async def test_untainted_request_is_not_absorbed_by_a_tainted_pending_twin(user, fresh_registry) -> None:
    _mail_tool(fresh_registry)
    await approvals.create(user.id, None, "mail_send", {**MAIL, "body": "click evil.example"}, "Mail",
                           utcnow() + timedelta(hours=48), tainted=True)
    [lc] = fresh_registry.for_agent("conversation", user.id)
    out = await lc.ainvoke({**MAIL, "body": "Hi Raj, see you Monday."})
    assert out.startswith("QUEUED_FOR_APPROVAL #")
    rows = await approvals.open_for_user(user.id)
    assert len(rows) == 2 and [r.tainted for r in rows] == [True, False]


async def test_two_untainted_equivalents_are_one_approval(user, fresh_registry) -> None:
    _mail_tool(fresh_registry)
    existing = await approvals.create(user.id, None, "mail_send", {**MAIL, "body": "Hi Raj"}, "Mail",
                                      utcnow() + timedelta(hours=48))
    [lc] = fresh_registry.for_agent("conversation", user.id)
    out = await lc.ainvoke({**MAIL, "body": "Hello Raj,"})
    assert out.startswith(f"ALREADY_AWAITING_APPROVAL #{existing}")
    assert len(await approvals.open_for_user(user.id)) == 1


async def test_tainted_run_records_taint_and_dedupes_only_with_tainted_twins(user, fresh_registry) -> None:
    from mavis.tools.registry import ToolRun, current_run

    _mail_tool(fresh_registry)
    clean = await approvals.create(user.id, None, "mail_send", {**MAIL, "body": "Hi"}, "Mail",
                                   utcnow() + timedelta(hours=48))
    token = current_run.set(ToolRun(tainted=True))
    try:
        [lc] = fresh_registry.for_agent("conversation", user.id)
        first = await lc.ainvoke({**MAIL, "body": "evil"})
        second = await lc.ainvoke({**MAIL, "body": "evil again"})
    finally:
        current_run.reset(token)
    assert first.startswith("QUEUED_FOR_APPROVAL #") and not first.startswith(f"QUEUED_FOR_APPROVAL #{clean}")
    assert second.startswith("ALREADY_AWAITING_APPROVAL #")  # absorbed by the tainted twin, not the clean one
    rows = await approvals.open_for_user(user.id)
    assert [r.tainted for r in rows] == [False, True]


# --- review round 4: relative days, periods and number words count when both goals name one -------
@pytest.mark.parametrize("a,b", [
    ("Book a table at Toit Indiranagar for a team lunch today at 3 PM with a window seat",
     "Book a table at Toit Indiranagar for a team lunch tomorrow at 3 PM with a window seat"),
    ("Book a table for two at Toit Indiranagar for dinner on Friday at 8pm near the window",
     "Book a table for four at Toit Indiranagar for dinner on Friday at 8pm near the window"),
    ("Plan my gym workouts, meals and grocery list for this week around my office schedule",
     "Plan my gym workouts, meals and grocery list for next week around my office schedule"),
])
def test_relative_day_period_and_number_words_keep_goals_apart(a, b) -> None:
    assert not same_goal(a, b)


def test_a_relative_word_on_one_side_only_does_not_split_the_incident_pair() -> None:
    assert "today" in INCIDENT_A.lower() and "today" not in INCIDENT_B.lower()
    assert same_goal(INCIDENT_A, INCIDENT_B)
    assert same_goal("Book a table for two at Toit tonight", "book a table for two at Toit tonight!")
