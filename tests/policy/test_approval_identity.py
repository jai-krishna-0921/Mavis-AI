"""A2: approvals close on evidence (an executed sibling with the same identity) and on time (an action
whose own time has passed). Identity and action time are declared by each tool, in one place; nothing
here is keyed on a tool's name."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from mavis.domain.policy import RiskClass
from mavis.domain.tasks import ApprovalStatus, TaskStatus
from mavis.domain.wakeups import WakeupKind
from mavis.policy import approvals as flow
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks
from mavis.tools.registry import MavisTool, get_registry

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # 17:30 IST


def _real(name: str) -> MavisTool:
    return get_registry().get(name)


# --- identity: declared by the tool, used everywhere ------------------------------------------------


def _invite(summary="Interview with JK", description="", start="2026-10-09T15:00:00+05:30",
            attendees=("jk@example.com",)) -> dict:
    return {"summary": summary, "start": start, "duration_minutes": 30, "attendees": list(attendees),
            "description": description}


@pytest.mark.parametrize("a,b,same", [
    (_invite(), _invite(description="Looking forward!"), True),                         # not part of WHAT
    (_invite(), _invite(start="2026-10-09T09:30:00Z"), True),                          # same instant
    (_invite(), _invite(attendees=("JK@Example.com",)), True),                         # case
    (_invite(attendees=("a@x.io", "b@x.io")), _invite(attendees=("b@x.io", "a@x.io")), True),  # order
    (_invite(), _invite(summary="Interview"), False),                                  # retitled: content
    (_invite(), {**_invite(), "duration_minutes": 60}, False),                         # corrected length
    (_invite(), _invite(start="2026-10-09T16:00:00+05:30"), False),
    (_invite(), _invite(attendees=("someone@else.org",)), False),
])
def test_calendar_create_identity_is_what_the_event_will_be(a, b, same):
    assert approvals.equivalent(a, b, _real("calendar_create_event").identity) is same


@pytest.mark.parametrize("a,b,same", [
    ({"to": ["raj@x.io"], "subject": "Offer", "body": "Hi Raj"},
     {"to": ["raj@x.io"], "subject": "offer ", "body": " hi  raj"}, True),               # formatting only
    ({"to": ["raj@x.io"], "subject": "Offer", "body": "Hi"},
     {"to": ["raj@x.io"], "subject": "Offer", "body": "Hello Raj, attached."}, False),  # body is content
    ({"to": ["raj@x.io"], "subject": "Offer", "body": "Hi"},
     {"to": ["raj@x.io"], "subject": "Offer", "body": "Hi", "cc": ["boss@x.io"]}, False),
    ({"to": ["raj@x.io"], "subject": "Offer", "body": "Hi"},
     {"to": ["mia@x.io"], "subject": "Offer", "body": "Hi"}, False),
])
def test_mail_send_identity_is_the_whole_message(a, b, same):
    assert approvals.equivalent(a, b, _real("mail_send").identity) is same


@pytest.mark.parametrize("a,b,same", [
    ({"event_id": "e1", "start": "2026-10-09T15:00:00+05:30"},
     {"event_id": "e1", "start": "2026-10-09T09:30:00Z"}, True),
    ({"event_id": "e1", "start": "2026-10-09T15:00:00+05:30", "summary": "A"},
     {"event_id": "e1", "start": "2026-10-09T15:00:00+05:30", "summary": "B"}, False),
    ({"event_id": "e1", "summary": "Rename"}, {"event_id": "e1", "start": "2026-10-09T15:00:00Z"}, False),
])
def test_calendar_update_identity_is_the_event_and_every_changed_field(a, b, same):
    assert approvals.equivalent(a, b, _real("calendar_update_event").identity) is same


@pytest.mark.parametrize("target,a,b,same", [
    (("start", "attendees"), _invite(), _invite(summary="Other", description="x"), True),
    (("start", "attendees"), _invite(), _invite(start="2026-10-10T15:00:00+05:30"), False),
    (("start", "attendees"), _invite(attendees=()), _invite(attendees=()), False),  # incomplete: no target
    (("to", "subject"), {"to": ["a@x.io"], "subject": "S", "body": "1"}, {"to": ["A@x.io"], "subject": "s",
                                                                          "body": "2"}, True),
    ((), {"text": "a"}, {"text": "a"}, False),                                       # no target declared
])
def test_same_target_is_a_coarser_declared_key(target, a, b, same):
    ka, kb = approvals.target_key(a, target), approvals.target_key(b, target)
    assert (ka is not None and ka == kb) is same


@pytest.mark.parametrize("identity,a,b,same", [
    (("account", "amount"), {"account": "ACME-1", "amount": 50, "memo": "rent"},
     {"account": "acme-1", "amount": 50, "memo": "October rent"}, True),
    (("account", "amount"), {"account": "ACME-1", "amount": 50}, {"account": "ACME-1", "amount": 60}, False),
    ((), {"text": "hi"}, {"text": " HI "}, True),       # no declared identity: canonical arguments
    ((), {"text": "hi"}, {"text": "bye"}, False),
])
def test_identity_is_whatever_the_tool_declares(identity, a, b, same):
    assert approvals.equivalent(a, b, identity) is same


def test_names_carry_no_meaning():
    """A tool that happens to be called calendar_create_event but declares nothing compares in full."""
    assert not approvals.equivalent(_invite(), _invite(description="Other"), ())


def test_identity_and_target_declarations_live_on_the_tools():
    reg = get_registry()
    names = ("calendar_create_event", "calendar_update_event", "mail_send")
    create, update, mail = (reg.get(n) for n in names)
    assert create.identity == ("start", "duration_minutes", "attendees", "summary")
    assert create.target == ("start", "attendees") and create.action_time == "start"
    assert update.identity == () and update.target == ("event_id",) and update.action_time == "start"
    assert mail.identity == ("to", "cc", "subject", "body") and mail.target == ("to", "subject")
    assert mail.action_time is None


# --- (a) an executed approval supersedes its siblings, whatever their wording or taint ---------------


class PayArgs(BaseModel):
    account: str
    amount: int
    memo: str = ""
    at: datetime | None = None


@pytest.fixture
def tools(fresh_registry):
    async def _run(user_id, args):
        return "ok"

    fresh_registry.register(MavisTool("pay_bill", "Pay a bill.", PayArgs, RiskClass.SPEND, _run,
                                      agents=frozenset({"conversation"}), identity=("account", "amount"),
                                      action_time="at", target=("account",)))
    for spec in ("calendar_create_event", "mail_send", "calendar_update_event"):
        fresh_registry.register(get_registry_default().get(spec))
    return fresh_registry


def get_registry_default():
    from mavis.tools import load_builtin_tools
    from mavis.tools.registry import ToolRegistry

    reg = ToolRegistry()
    load_builtin_tools(reg)
    return reg


async def _card(user_id, tool, args, *, tainted=False, task_status=None):
    tid = None
    if task_status is not None:
        tid = await tasks.create(user_id, goal="g")
        await tasks.set_status(tid, task_status)
    return await approvals.create(user_id, tid, tool, args, f"{tool} preview", utcnow() + timedelta(hours=48),
                                  tainted=tainted)


CASES = [  # tool, executed args, exact twin (formatting only), same target but other content, unrelated
    ("calendar_create_event", _invite(), _invite(attendees=("JK@example.com",), description="see you"),
     _invite(summary="Interview (rescheduled)"), _invite(start="2026-10-10T15:00:00+05:30")),
    ("mail_send", {"to": ["raj@x.io"], "subject": "Offer", "body": "Hi"},
     {"to": ["RAJ@x.io"], "subject": "offer", "body": " Hi "},
     {"to": ["raj@x.io"], "subject": "Offer", "body": "Hello Raj, v2"},
     {"to": ["raj@x.io"], "subject": "Contract", "body": "Hi"}),
    ("calendar_update_event",
     {"event_id": "e1", "start": "2026-10-09T15:00:00+05:30", "duration_minutes": 30},
     {"event_id": "e1", "start": "2026-10-09T09:30:00Z", "duration_minutes": 30},
     {"event_id": "e1", "start": "2026-10-09T17:00:00+05:30", "duration_minutes": 30},
     {"event_id": "e2", "start": "2026-10-09T15:00:00+05:30", "duration_minutes": 30}),
    ("pay_bill", {"account": "A1", "amount": 50, "memo": "rent"}, {"account": "a1", "amount": 50},
     {"account": "A1", "amount": 75}, {"account": "B2", "amount": 50}),
]


@pytest.mark.parametrize("tool,executed,twin,variant,other", CASES)
async def test_execution_supersedes_exact_twins_only_and_notes_variants(user, rec_bus, sent, tools, tool,
                                                                        executed, twin, variant, other):
    done = await _card(user.id, tool, executed)
    clean_twin = await _card(user.id, tool, twin)
    tainted_twin = await _card(user.id, tool, twin, tainted=True)
    waiting_twin = await _card(user.id, tool, twin, task_status=TaskStatus.AWAITING_APPROVAL)
    variant_card = await _card(user.id, tool, variant)
    unrelated = await _card(user.id, tool, other)
    await approvals.set_status(done, ApprovalStatus.EXECUTED, "ok")
    await flow.supersede_duplicates(await approvals.get(done), executed=True)
    for aid in (clean_twin, tainted_twin):
        row = await approvals.get(aid)
        assert row.status == ApprovalStatus.REJECTED and "superseded" in row.result
    assert (await approvals.get(waiting_twin)).status == ApprovalStatus.RESOLVING  # its task is resumed
    assert (await approvals.get(variant_card)).status == ApprovalStatus.PENDING  # different content stays
    assert any("a version of this was already sent" in m.text.lower()
               and f"ap:{variant_card}:ok" in str(m.buttons) for m in sent)
    assert (await approvals.get(unrelated)).status == ApprovalStatus.PENDING
    assert not any(f"ap:{unrelated}:" in str(m.buttons) for m in sent)


async def test_rejection_only_closes_exact_twins_of_the_same_taint(user, rec_bus, sent, tools):
    msg = {"to": ["raj@x.io"], "subject": "Offer", "body": "x"}
    rejected = await _card(user.id, "mail_send", msg)
    clean = await _card(user.id, "mail_send", {**msg, "body": " X "})
    tainted = await _card(user.id, "mail_send", {**msg, "body": "x"}, tainted=True)
    variant = await _card(user.id, "mail_send", {**msg, "body": "y"})
    await approvals.set_status(rejected, ApprovalStatus.REJECTED)
    await flow.supersede_duplicates(await approvals.get(rejected), executed=False)
    assert (await approvals.get(clean)).status == ApprovalStatus.REJECTED
    assert (await approvals.get(tainted)).status == ApprovalStatus.PENDING
    assert (await approvals.get(variant)).status == ApprovalStatus.PENDING


@pytest.mark.parametrize("tool,executed,twin,variant,other", CASES)
async def test_queue_reports_exact_twins_and_updates_same_target_cards(user, rec_bus, sent, tools, tool,
                                                                       executed, twin, variant, other):
    """An exact twin is reported as already waiting; a corrected version of a waiting card UPDATES that
    card (args, preview, re-sent with buttons, audited) instead of a duplicate or a second card."""
    from sqlalchemy import select

    from mavis.store.db import Session
    from mavis.store.models import AuditLog

    existing = await _card(user.id, tool, executed)
    agent = sorted(tools.get(tool).agents)[0]
    [lc] = [t for t in tools.for_agent(agent, user.id) if t.name == tool]
    out = await lc.ainvoke(twin)
    assert out.startswith(f"ALREADY_AWAITING_APPROVAL #{existing}")
    out = await lc.ainvoke(variant)
    assert out.startswith(f"UPDATED_WAITING_APPROVAL #{existing}")
    row = await approvals.get(existing)
    assert row.status == ApprovalStatus.PENDING
    assert approvals.equivalent(row.arguments, tools.get(tool).args_model.model_validate(variant).model_dump(
        mode="json"), ())
    assert any(f"ap:{existing}:ok" in str(m.buttons) for m in sent)  # the corrected card is shown again
    async with Session() as s:
        actions = [a.action for a in await s.scalars(select(AuditLog))]
    assert "approval.updated" in actions
    out = await lc.ainvoke(other)
    assert out.startswith("QUEUED_FOR_APPROVAL #")
    assert len(await approvals.open_for_user(user.id)) == 2


async def test_a_card_being_edited_is_not_overwritten_by_a_same_target_request(user, rec_bus, sent, tools):
    existing = await _card(user.id, "pay_bill", {"account": "A1", "amount": 50})
    await approvals.claim(existing, {ApprovalStatus.PENDING}, ApprovalStatus.AWAITING_EDIT)
    [lc] = [t for t in tools.for_agent("conversation", user.id) if t.name == "pay_bill"]
    out = await lc.ainvoke({"account": "A1", "amount": 80})
    assert out.startswith("QUEUED_FOR_APPROVAL #")
    assert (await approvals.get(existing)).arguments["amount"] == 50


async def test_a_tainted_request_never_updates_a_clean_card(user, rec_bus, sent, tools):
    from mavis.tools.registry import ToolRun, current_run

    existing = await _card(user.id, "pay_bill", {"account": "A1", "amount": 50})
    token = current_run.set(ToolRun(tainted=True))
    try:
        [lc] = [t for t in tools.for_agent("conversation", user.id) if t.name == "pay_bill"]
        out = await lc.ainvoke({"account": "A1", "amount": 9999})
    finally:
        current_run.reset(token)
    assert out.startswith("QUEUED_FOR_APPROVAL #")
    assert (await approvals.get(existing)).arguments["amount"] == 50


# --- (b) an action whose own time has passed expires; approving it is refused ------------------------


@pytest.mark.parametrize("tool,args,expired", [
    ("calendar_create_event", _invite(start="2026-10-03T15:00:00+05:30"), True),     # 15:00 IST < 17:30
    ("calendar_create_event", _invite(start="2026-10-03T18:00:00+05:30"), False),
    ("calendar_create_event", _invite(start="2026-10-03T15:00:00"), True),           # naive: user's tz
    ("calendar_create_event", _invite(start="2026-10-03T13:00:00"), True),
    ("calendar_create_event", _invite(start="2026-10-03T19:00:00"), False),          # 19:00 IST, not UTC
    ("calendar_update_event", {"event_id": "e1", "start": "2026-10-02T10:00:00Z"}, True),
    ("calendar_update_event", {"event_id": "e1", "summary": "Renamed"}, False),     # no time component
    ("pay_bill", {"account": "A", "amount": 1, "at": "2026-10-03T11:59:00Z"}, True),
    ("pay_bill", {"account": "A", "amount": 1, "at": "2026-10-03T12:30:00Z"}, False),
    ("mail_send", {"to": ["x@y.z"], "subject": "s", "body": "b"}, False),            # declares no time
])
async def test_sweep_expires_open_approvals_whose_action_time_passed(user, rec_bus, sent, clock, tools,
                                                                     tool, args, expired):
    clock.set(NOW)
    aid = await _card(user.id, tool, args)
    await flow.sweep(user.id)
    row = await approvals.get(aid)
    if expired:
        assert row.status == ApprovalStatus.EXPIRED
        assert any("already passed" in m.text for m in sent)
    else:
        assert row.status == ApprovalStatus.PENDING


async def test_remind_on_a_past_action_expires_instead(user, rec_bus, sent, clock, tools):
    clock.set(NOW)
    aid = await _card(user.id, "calendar_create_event", _invite(start="2026-10-03T09:00:00Z"))
    await flow.remind(user.id, aid)
    assert not any("Still want me to go ahead" in m.text for m in sent)
    assert (await approvals.get(aid)).status == ApprovalStatus.EXPIRED


async def test_prompt_schedules_an_expiry_at_the_action_time(user, rec_bus, sent, clock, tools):
    from mavis.timers.service import WakeupService

    clock.set(NOW)
    start = NOW + timedelta(hours=3)
    aid = await _card(user.id, "pay_bill", {"account": "A", "amount": 1, "at": start.isoformat()})
    await flow.send_approval_prompt(user.id, {"approval_id": aid})
    due = sorted(w.due_at for w in await WakeupService().pending(user.id, WakeupKind.SYSTEM_APPROVAL_EXPIRE))
    assert start in due


async def test_approving_a_past_action_is_refused_with_a_clear_message(
    user, fake_llm, rec_bus, sent, tools, memory_checkpointer, monkeypatch, clock
):
    """The gate re-checks at execution: a past-time action never runs, whichever path approved it."""
    from mavis.agents import orchestrator
    from mavis.domain.tasks import TaskKind

    clock.set(NOW)
    ran = []

    async def _run(user_id, args):
        ran.append(args)
        return "paid"

    tools._tools["pay_bill"] = MavisTool("pay_bill", "Pay a bill.", PayArgs, RiskClass.SPEND, _run,
                                         agents=frozenset({"conversation"}), identity=("account", "amount"),
                                         action_time="at")
    tid = await tasks.create(user.id, goal="approve", kind=TaskKind.APPROVAL)
    soon = (NOW + timedelta(minutes=5)).isoformat()
    aid = await approvals.create(user.id, tid, "pay_bill", {"account": "A", "amount": 5, "at": soon},
                                 "Pay A 5", NOW + timedelta(hours=48))
    await orchestrator.run_task(tid)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    clock.set(NOW + timedelta(minutes=10))  # the user taps Send after the time has gone
    await orchestrator.resume_task(tid, {"approval_id": aid, "decision": "ok"})
    assert ran == []
    assert (await approvals.get(aid)).status == ApprovalStatus.EXPIRED
    assert "already passed" in (await tasks.get(tid)).result_text


# --- fix round 1, C1: the action-time expiry follows the CURRENT arguments ------------------------------


def _times(tool: str, at: str) -> dict:
    return {"calendar_create_event": _invite(start=at),
            "calendar_update_event": {"event_id": "e9", "start": at},
            "pay_bill": {"account": "A", "amount": 3, "at": at}}[tool]


async def _action_wakeups(user_id: int, aid: int) -> list[datetime]:
    from mavis.timers.service import WakeupService

    return sorted(w.due_at for w in await WakeupService().pending(user_id, WakeupKind.SYSTEM_APPROVAL_EXPIRE)
                  if w.reason == f"approval:{aid}:action_time")


@pytest.mark.parametrize("tool", ["calendar_create_event", "calendar_update_event", "pay_bill"])
@pytest.mark.parametrize("first,second", [
    ("2026-10-03T14:00:00Z", "2026-10-04T09:00:00Z"),        # moved later
    ("2026-10-03T20:00:00+05:30", "2026-10-03T19:00:00+05:30"),  # moved earlier (IST offsets)
    ("2026-10-03T21:00:00", "2026-10-03T23:00:00"),            # naive: the user's timezone
])
async def test_edit_moves_the_action_time_expiry(user, rec_bus, sent, clock, tools, tool, first, second):
    clock.set(NOW)
    aid = await _card(user.id, tool, _times(tool, first))
    await flow.send_approval_prompt(user.id, {"approval_id": aid})
    assert len(await _action_wakeups(user.id, aid)) == 1
    await approvals.update_args(aid, _times(tool, second), "edited")
    await flow.send_approval_prompt(user.id, {"approval_id": aid})  # the gate re-prompts after an edit
    row = await approvals.get(aid)
    tz = "Asia/Kolkata"
    assert await _action_wakeups(user.id, aid) == [flow.action_time(row, tz)]


@pytest.mark.parametrize("tool", ["calendar_create_event", "pay_bill"])
async def test_stale_action_time_wakeup_does_nothing_when_the_time_moved(user, rec_bus, sent, clock, tools,
                                                                        tool):
    clock.set(NOW)
    aid = await _card(user.id, tool, _times(tool, "2026-10-03T12:30:00Z"))
    await approvals.update_args(aid, _times(tool, "2026-10-04T12:30:00Z"), "edited")
    clock.set(NOW + timedelta(hours=1))  # the old time has passed, the new one has not
    await flow.on_expire_wakeup(user.id, f"approval:{aid}:action_time")
    assert (await approvals.get(aid)).status == ApprovalStatus.PENDING


@pytest.mark.parametrize("tool", ["calendar_create_event", "calendar_update_event", "pay_bill"])
async def test_action_time_wakeup_never_claims_a_card_being_edited(user, rec_bus, sent, clock, tools, tool):
    clock.set(NOW)
    aid = await _card(user.id, tool, _times(tool, "2026-10-03T11:00:00Z"))
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.AWAITING_EDIT)
    await flow.on_expire_wakeup(user.id, f"approval:{aid}:action_time")
    assert (await approvals.get(aid)).status == ApprovalStatus.AWAITING_EDIT


@pytest.mark.parametrize("tool", ["calendar_create_event", "calendar_update_event", "pay_bill"])
async def test_action_time_wakeup_expires_a_pending_card_whose_time_passed(user, rec_bus, sent, clock, tools,
                                                                          tool):
    clock.set(NOW)
    aid = await _card(user.id, tool, _times(tool, "2026-10-03T11:59:00Z"))
    await flow.on_expire_wakeup(user.id, f"approval:{aid}:action_time")
    assert (await approvals.get(aid)).status == ApprovalStatus.EXPIRED
    assert any("already passed" in m.text for m in sent)


async def test_revise_reschedules_the_action_time_expiry(user, rec_bus, sent, clock, tools, fake_llm):
    from mavis.agents.orchestrator_graph import revise_approval

    clock.set(NOW)
    aid = await _card(user.id, "pay_bill", _times("pay_bill", "2026-10-03T13:00:00Z"))
    await flow.send_approval_prompt(user.id, {"approval_id": aid})
    fake_llm.push_structured(PayArgs(account="A", amount=3, at=datetime(2026, 10, 3, 16, 0, tzinfo=UTC)))
    await revise_approval(await approvals.get(aid), "make it 4pm UTC")
    assert await _action_wakeups(user.id, aid) == [datetime(2026, 10, 3, 16, 0, tzinfo=UTC)]
