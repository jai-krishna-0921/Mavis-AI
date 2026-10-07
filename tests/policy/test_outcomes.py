"""H1: truthful terminal outcomes; "recently failed" is computed from rows; a failed action's loops stop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mavis.agents import orchestrator_graph as og
from mavis.domain import timeutil
from mavis.domain.events import Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.messages import Role
from mavis.domain.tasks import ApprovalStatus, TaskKind, TaskStatus
from mavis.loops.service import LoopService
from mavis.policy import outcomes
from mavis.store.repo import approvals, messages, tasks
from mavis.store.repo import loops as loops_repo

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
START = "2026-10-06T14:00:00+05:30"


@pytest.fixture
def at_now(clock):
    clock.set(NOW)
    return clock


async def _approval(user_id, tool, args, preview, status, *, reason=None, turn=None,
                    task_kind=TaskKind.APPROVAL, tainted=False) -> int:
    tid = await tasks.create(user_id, goal="approve", kind=task_kind, turn_ref=turn)
    aid = await approvals.create(user_id, tid, tool, args, preview, timeutil.now() + timedelta(hours=48),
                                 tainted=tainted)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.EXECUTED)
    if status is ApprovalStatus.FAILED:
        await approvals.set_status(aid, ApprovalStatus.FAILED, result='calendar failed: {"error": 400}',
                                   from_statuses={ApprovalStatus.EXECUTED}, failure_reason=reason)
    else:
        await approvals.set_status(aid, ApprovalStatus.EXECUTED, result="ok",
                                   from_statuses={ApprovalStatus.EXECUTED})
    return aid


async def _task(user_id, goal, status, error=None) -> int:
    tid = await tasks.create(user_id, goal=goal)
    await tasks.claim(tid, TaskStatus.QUEUED, status, result_text="report", error=error)
    return tid


# --- terminal status derived from what happened ---------------------------------------------------------


@pytest.mark.parametrize("kind,outcomes_,results,expected", [
    (TaskKind.APPROVAL, ["failed"], {}, TaskStatus.FAILED),
    (TaskKind.APPROVAL, ["past"], {}, TaskStatus.FAILED),
    (TaskKind.APPROVAL, ["executed", "failed"], {}, TaskStatus.PARTIAL),
    (TaskKind.APPROVAL, ["executed"], {}, TaskStatus.DONE),
    (TaskKind.APPROVAL, ["rejected", "expired", "superseded"], {}, TaskStatus.DONE),  # user's decisions
    (TaskKind.TASK, [], {"s1": {"ok": True}, "s2": {"ok": True}}, TaskStatus.DONE),
    (TaskKind.TASK, [], {"s1": {"ok": True, "partial": True}}, TaskStatus.PARTIAL),
    (TaskKind.TASK, [], {"s1": {"ok": True}, "s2": {"ok": False}}, TaskStatus.PARTIAL),
    (TaskKind.TASK, [], {"s1": {"ok": False, "partial": True}}, TaskStatus.FAILED),  # wrap-up said nothing
    (TaskKind.TASK, [], {"s1": {"ok": False}, "s2": {"ok": False}}, TaskStatus.FAILED),
    (TaskKind.TASK, ["failed"], {"s1": {"ok": True}}, TaskStatus.PARTIAL),
    (TaskKind.TASK, ["executed"], {"s1": {"ok": False}}, TaskStatus.PARTIAL),  # the action did happen
])
def test_status_is_derived_from_steps_and_approvals(kind, outcomes_, results, expected):
    state = {"kind": kind, "approval_outcomes": [{"status": s} for s in outcomes_], "results": results}
    status, reason = og.derive_outcome(state)
    assert status is expected
    if expected is not TaskStatus.DONE and results:
        assert reason and "{" not in reason


# --- recently failed: one computed list ----------------------------------------------------------------


async def test_failed_approvals_and_tasks_are_listed_with_plain_reasons(user, at_now):
    cal = await _approval(user.id, "calendar_create_event", {"summary": "Focus", "start": START},
                          "📅 Focus\nTue 06 Oct", ApprovalStatus.FAILED,
                          reason="Google Calendar did not accept some of the details")
    mail = await _approval(user.id, "mail_send", {"to": ["a@x.io"], "subject": "Q3", "body": "b"},
                           "✉️ To: a@x.io", ApprovalStatus.FAILED)  # older row: no reason recorded
    research = await _task(user.id, "compare standing desks", TaskStatus.PARTIAL, "only part of it got done")
    crashed = await _task(user.id, "summarise the board deck", TaskStatus.FAILED,
                          "something broke on my side")
    await _task(user.id, "book a table", TaskStatus.DONE)
    at_now.advance(minutes=5)
    items = await outcomes.recently_failed(user.id)
    assert [i.ref for i in items] == [f"approval:{cal}", f"approval:{mail}", f"task:{research}",
                                     f"task:{crashed}"]
    assert items[0].summary == "📅 Focus" and items[0].reason.startswith("Google Calendar")
    assert items[1].reason == outcomes.NOT_THROUGH
    assert items[2].outcome == "partly done" and items[3].outcome == "failed"
    text, untrusted = outcomes.render_recently_failed(items, timeutil.now(), "Asia/Kolkata")
    assert text.startswith(outcomes.HEADING) and "{" not in text and not untrusted
    assert all(d not in text for d in ("—", "–"))


async def test_window_acknowledgement_and_later_success_end_an_item(user, at_now):
    old = await _approval(user.id, "slack_send", {"channel": "#ops", "text": "deploy"}, "💬 #ops",
                          ApprovalStatus.FAILED)
    at_now.advance(hours=49)
    acked = await _approval(user.id, "mail_send", {"to": ["k@x.io"], "subject": "Hi", "body": "b"}, "✉️ k",
                            ApprovalStatus.FAILED)
    retried = await _approval(user.id, "calendar_create_event",
                              {"summary": "Block", "start": START, "attendees": ["bad@x.io"]}, "📅 Block",
                              ApprovalStatus.FAILED)
    other = await _approval(user.id, "calendar_create_event", {"summary": "Gym", "start": "2026-10-07T07:00"},
                            "📅 Gym", ApprovalStatus.FAILED)
    failed_task = await _task(user.id, "Find flights to Goa", TaskStatus.FAILED)
    at_now.advance(minutes=1)
    # the same subject succeeded later: the calendar retry without the guest, the task asked again
    await _approval(user.id, "calendar_create_event", {"summary": "Block", "start": START}, "📅 Block",
                    ApprovalStatus.EXECUTED)
    await _task(user.id, "find  flights to goa", TaskStatus.DONE)
    assert await outcomes.acknowledge(user.id, [f"approval:{acked}", "nonsense", "task:x"]) == 1
    refs = [i.ref for i in await outcomes.recently_failed(user.id)]
    assert refs == [f"approval:{other}"]
    assert f"approval:{old}" not in refs and f"approval:{retried}" not in refs
    assert f"task:{failed_task}" not in refs


async def test_task_approvals_are_reported_with_their_task_not_twice(user, at_now):
    await _approval(user.id, "mail_send", {"to": ["a@x.io"], "subject": "s", "body": "b"}, "✉️ a",
                    ApprovalStatus.FAILED, task_kind=TaskKind.TASK)
    tainted = await _approval(user.id, "docs_create", {"title": "Notes"}, "📄 Notes from inbox",
                              ApprovalStatus.FAILED, tainted=True)
    items = await outcomes.recently_failed(user.id)
    assert [i.ref for i in items] == [f"approval:{tainted}"]
    text, untrusted = outcomes.render_recently_failed(items, timeutil.now(), "UTC", source="pending")
    assert untrusted and '<untrusted source="pending">' in text


async def test_pending_tool_shows_recently_failed_first_and_acknowledge_tool_clears(user, at_now, rec_bus):
    from mavis.tools import assistant

    aid = await _approval(user.id, "calendar_create_event", {"summary": "Dentist", "start": START},
                          "📅 Dentist", ApprovalStatus.FAILED, reason="Google Calendar refused access")
    await LoopService(rec_bus).upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Send the deck",
                                                       trust=Trust.USER))
    out = await assistant.pending(user.id, assistant.PendingArgs())
    assert out.index(outcomes.HEADING) < out.index("Open items")
    assert f"[approval:{aid}]" in out and "Google Calendar refused access" in out
    ack = await assistant.acknowledge_failure(user.id, assistant.AcknowledgeArgs(refs=[f"approval:{aid}"]))
    assert ack.user_text
    assert outcomes.HEADING not in await assistant.pending(user.id, assistant.PendingArgs())


async def test_morning_brief_carries_recently_failed(user, at_now, recording_bus, fake_memory, fake_llm,
                                                    monkeypatch):
    from mavis.domain.decisions import ComposedMessage
    from mavis.initiative import routines as routines_mod
    from mavis.initiative.composer import Composer
    from tests.initiative.test_routines import build

    routines_mod.clear_brief_sources()
    await _task(user.id, "compare standing desks", TaskStatus.PARTIAL, "only part of it got done")
    seen = {}

    async def spy(self, user_, intent, urgency, context="", **kw):
        seen["intent"] = intent
        return ComposedMessage(send=False, messages=[])

    monkeypatch.setattr(Composer, "compose", spy)
    routines, _, _ = build(recording_bus, fake_memory)
    await routines.run(user, {"routine": routines_mod.MORNING_ROUTINE, "loop_id": None})
    assert f"{outcomes.HEADING}: compare standing desks: partly done" in seen["intent"]


# --- a failed action's loops are not live ----------------------------------------------------------------


async def _turns(user_id: int, *event_ids: str) -> None:
    for e in event_ids:
        await messages.log(user_id, Role.USER, f"message {e}", event_id=e)
        await messages.log(user_id, Role.ASSISTANT, f"reply {e}", event_id=f"reply:{e}")


async def _loop(user_id, title, source, origin=LoopOrigin.CONVERSATION):
    from mavis.bus import get_bus

    return await LoopService(get_bus()).upsert(user_id, LoopUpsert(
        kind=LoopKind.COMMITMENT, title=title, source=source, origin=origin, trust=Trust.USER,
        due_at=NOW + timedelta(days=1)))


async def test_failed_approval_blocks_loops_of_its_turn_and_the_one_before(user, at_now, rec_bus):
    await _turns(user.id, "tg:1", "tg:2", "tg:3", "tg:4")
    before = await _loop(user.id, "Call the plumber", "tg:1")       # two turns back: unrelated
    asked = await _loop(user.id, "Block 2 pm Tuesday", "tg:2")       # the request
    same = await _loop(user.id, "Lunch with Asha Tuesday", "tg:3")   # the approval's own turn
    later = await _loop(user.id, "Pay rent", "tg:4")                 # after it
    mine = await _loop(user.id, "Check in about the block", "tg:3", origin=LoopOrigin.REASONER)
    aid = await _approval(user.id, "calendar_create_event", {"summary": "Block", "start": START}, "📅 Block",
                          ApprovalStatus.FAILED, turn="tg:3")
    assert await outcomes.block_loops_of_failed_approval(aid) == 2
    status = {lp.id: (await loops_repo.get(lp.id)).status for lp in (before, asked, same, later, mine)}
    assert status == {before.id: LoopStatus.OPEN, asked.id: LoopStatus.BLOCKED, same.id: LoopStatus.BLOCKED,
                      later.id: LoopStatus.OPEN, mine.id: LoopStatus.OPEN}
    assert asked.id not in {lp.id for lp in await LoopService(rec_bus).active(user.id)}


async def test_learn_after_the_failure_creates_a_blocked_loop(user, at_now, rec_bus):
    from mavis.domain.events import Provenance
    from mavis.domain.memory import Extraction, LoopDraft
    from mavis.loops.service import loops_from_extraction

    await _turns(user.id, "cli:a", "cli:b", "cli:c")
    await _approval(user.id, "mail_send", {"to": ["v@x.io"], "subject": "Invoice", "body": "b"}, "✉️ v",
                    ApprovalStatus.FAILED, turn="cli:b")
    service = LoopService(rec_bus)
    for source, title in (("cli:b", "Send the invoice to Vik"), ("cli:c", "Renew passport")):
        extraction = Extraction(loops=[LoopDraft(kind="commitment", title=title)])
        await loops_from_extraction(service, user.id, extraction,
                                    Provenance(source_ref=source, trust=Trust.USER, conversation=True))
    by_title = {lp.title: lp.status for lp in await loops_repo.list_from_sources(
        user.id, ["cli:b", "cli:c"], tuple(LoopStatus))}
    assert by_title == {"Send the invoice to Vik": LoopStatus.BLOCKED, "Renew passport": LoopStatus.OPEN}


async def test_approval_gate_failure_records_reason_blocks_loops_and_fails_the_task(
        user, fake_llm, rec_bus, fresh_registry):
    from langgraph.types import Command

    from mavis.domain.errors import ActionFailed, FailureKind
    from mavis.domain.policy import RiskClass
    from mavis.tools.registry import MavisTool
    from tests.agents.test_orchestrator_graph import _cfg, _graph
    from tests.conftest import SendNoteArgs

    async def _refused(user_id, args):
        raise ActionFailed('send failed: {"error": {"code": 404}}', reason="Slack could not find it",
                           kind=FailureKind.NOT_FOUND)

    fresh_registry.register(MavisTool(name="send_note", description="d", args_model=SendNoteArgs,
                                      risk=RiskClass.OUTWARD, fn=_refused,
                                      agents=frozenset({"conversation"})))
    await _turns(user.id, "tg:10", "tg:11")
    loop = await _loop(user.id, "Note to the team", "tg:11")
    tid = await tasks.create(user.id, goal="approve", kind=TaskKind.APPROVAL, turn_ref="tg:11")
    aid = await approvals.create(user.id, tid, "send_note", {"text": "x"}, "Send note: x",
                                 timeutil.now() + timedelta(hours=48))
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    await graph.ainvoke(Command(resume={"approval_id": aid, "decision": "ok"}), _cfg(tid))
    assert (await tasks.get(tid)).status == TaskStatus.FAILED
    assert (await approvals.get(aid)).failure_reason == "Slack could not find it"
    assert (await loops_repo.get(loop.id)).status is LoopStatus.BLOCKED
    [done] = [e for e in rec_bus.events if e.id == f"task:{tid}:completed"]
    assert done.payload["status"] == "failed"


async def test_reported_failed_task_is_not_announced_as_ran_after_stop(user, at_now):
    tid = await tasks.create(user.id, goal="g")
    aid = await approvals.create(user.id, tid, "mail_send", {"to": ["a@x.io"]}, "✉️",
                                 timeutil.now() + timedelta(hours=48))
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.EXECUTED)
    await approvals.set_status(aid, ApprovalStatus.EXECUTED, result="ok",
                               from_statuses={ApprovalStatus.EXECUTED})
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.FAILED, result_text="the report")
    assert await approvals.executed_after_stop(NOW - timedelta(hours=1), user.id) == []
    await tasks.claim(tid, TaskStatus.FAILED, TaskStatus.FAILED, result_text=None)
    assert [a.id for a in await approvals.executed_after_stop(NOW - timedelta(hours=1), user.id)] == [aid]
