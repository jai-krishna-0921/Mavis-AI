import re
from datetime import timedelta

from mavis.agents import buttons
from mavis.domain.events import Event, EventType, JobKind, Trust
from mavis.domain.tasks import ApprovalStatus, TaskStatus
from mavis.policy import approvals as flow
from mavis.store.db import Session, utcnow
from mavis.store.models import PendingApproval
from mavis.store.repo import approvals, tasks

_DASHES = re.compile("[–—]")


async def _pending(user_id: int) -> tuple[int, int]:
    tid = await tasks.create(user_id, goal="g")
    aid = await approvals.create(user_id, tid, "send_note", {"text": "hi"}, "Send note: hi",
                                 utcnow() + timedelta(hours=48))
    return tid, aid


def _press(user_id: int, data: str, n: int = 1) -> Event:
    return Event(id=f"tg:cb:{n}", user_id=user_id, type=EventType.BUTTON_PRESSED, occurred_at=utcnow(),
                 source="telegram", payload={"data": data}, trust=Trust.USER)


async def _force(aid: int, **fields) -> None:
    async with Session() as s:
        row = await s.get(PendingApproval, aid)
        for k, v in fields.items():
            setattr(row, k, v)
        await s.commit()


async def test_duplicate_button_press_enqueues_one_resume(user, rec_bus, sent):
    tid, aid = await _pending(user.id)
    await flow.handle_approval_button(_press(user.id, f"ap:{aid}:ok", 1))
    await flow.handle_approval_button(_press(user.id, f"ap:{aid}:ok", 2))
    resumes = [j for j in rec_bus.jobs if j.kind == JobKind.RESUME_TASK]
    assert len(resumes) == 1
    assert resumes[0].payload == {"task_id": tid, "approval_id": aid, "decision": "ok", "instructions": ""}
    assert (await approvals.get(aid)).status == ApprovalStatus.RESOLVING
    assert (await approvals.get(aid)).resolving_at is not None


async def test_cancel_button_resumes_with_no(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    await flow.handle_approval_button(_press(user.id, f"ap:{aid}:no"))
    assert rec_bus.jobs[0].payload["decision"] == "no"


async def test_edit_button_asks_for_changes(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    await flow.handle_approval_button(_press(user.id, f"ap:{aid}:edit"))
    assert (await approvals.get(aid)).status == ApprovalStatus.AWAITING_EDIT
    assert rec_bus.jobs == []
    assert "change" in sent[-1].text.lower()


async def test_button_for_other_users_approval_ignored(user, rec_bus, sent):
    from mavis.store.repo import users

    other, _ = await users.get_or_create_by_chat(777, "Mallory")
    _, aid = await _pending(user.id)
    await flow.handle_approval_button(_press(other.id, f"ap:{aid}:ok"))
    assert rec_bus.jobs == []
    assert (await approvals.get(aid)).status == ApprovalStatus.PENDING


async def test_malformed_button_data_ignored(user, rec_bus, sent):
    await flow.handle_approval_button(_press(user.id, "ap:abc:ok"))
    await flow.handle_approval_button(_press(user.id, "ap:1:delete_everything"))
    assert rec_bus.jobs == []


async def test_awaiting_edit_text_is_edit_without_llm(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.AWAITING_EDIT)
    interp = await flow.interpret_reply(await approvals.get(aid), "say it warmer")
    assert interp.decision == "edit" and interp.instructions == "say it warmer"


async def test_apply_reply_edit_enqueues_resume(user, rec_bus, sent, fake_llm):
    tid, aid = await _pending(user.id)
    fake_llm.push_structured(flow.ApprovalReplyInterpretation(decision="edit", instructions="more formal"))
    a = await approvals.get(aid)
    ack = await flow.apply_reply(a, await flow.interpret_reply(a, "make it more formal"))
    assert ack and "revising" in ack.lower()
    assert rec_bus.jobs[0].payload == {"task_id": tid, "approval_id": aid, "decision": "edit",
                                       "instructions": "more formal"}


async def test_apply_reply_unrelated_returns_none(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    a = await approvals.get(aid)
    assert await flow.apply_reply(a, flow.ApprovalReplyInterpretation(decision="unrelated")) is None
    assert rec_bus.jobs == []
    assert (await approvals.get(aid)).status == ApprovalStatus.PENDING


async def test_remind_only_when_pending(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    await flow.remind(user.id, aid)
    assert "Still want me" in sent[-1].text
    assert sent[-1].buttons
    assert sent[-1].dedupe_key == f"approval:{aid}:remind"
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    n = len(sent)
    await flow.remind(user.id, aid)
    assert len(sent) == n


async def test_expire_resumes_with_expired(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    await flow.expire(user.id, aid)
    assert rec_bus.jobs[0].payload["decision"] == "expired"
    await flow.expire(user.id, aid)
    assert len(rec_bus.jobs) == 1


def test_approval_id_from_reason():
    assert flow.approval_id_from_reason("approval:12") == 12
    assert flow.approval_id_from_reason("task:12") is None


# --- single winner, text replies ------------------------------------------------------------------


async def test_repeated_text_reply_is_a_noop_after_the_first(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    a = await approvals.get(aid)
    yes = flow.ApprovalReplyInterpretation(decision="approve")
    assert await flow.apply_reply(a, yes) == "On it."
    assert "already" in (await flow.apply_reply(a, yes)).lower()
    assert len(rec_bus.jobs) == 1


async def test_button_and_text_race_has_one_winner(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    a = await approvals.get(aid)
    await flow.handle_approval_button(_press(user.id, f"ap:{aid}:no"))
    ack = await flow.apply_reply(a, flow.ApprovalReplyInterpretation(decision="approve"))
    assert "already" in ack.lower()
    assert [j.payload["decision"] for j in rec_bus.jobs] == ["no"]


async def test_text_reply_asks_which_when_two_are_pending(user, rec_bus, sent):
    _, first = await _pending(user.id)
    _, second = await _pending(user.id)
    a = await approvals.get(first)
    ack = await flow.apply_reply(a, flow.ApprovalReplyInterpretation(decision="approve"))
    assert "which one" in ack.lower() and "1." in ack and "2." in ack
    assert rec_bus.jobs == []
    assert (await approvals.get(first)).status == ApprovalStatus.PENDING
    assert (await approvals.get(second)).status == ApprovalStatus.PENDING
    assert not _DASHES.search(ack)


async def test_edit_reply_goes_to_the_one_being_edited_even_with_others_pending(user, rec_bus, sent):
    _, first = await _pending(user.id)
    _, second = await _pending(user.id)
    await flow.handle_approval_button(_press(user.id, f"ap:{second}:edit"))
    a = await approvals.get(second)
    interp = await flow.interpret_reply(a, "shorter")
    assert await flow.apply_reply(a, interp) is not None
    assert [j.payload["approval_id"] for j in rec_bus.jobs] == [second]
    assert (await approvals.get(first)).status == ApprovalStatus.PENDING


async def test_tap_on_a_finished_approval_says_so_once(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    await approvals.set_status(aid, ApprovalStatus.EXECUTED, "done")
    await flow.handle_approval_button(_press(user.id, f"ap:{aid}:ok", 1))
    await flow.handle_approval_button(_press(user.id, f"ap:{aid}:ok", 2))
    assert rec_bus.jobs == []
    assert {m.dedupe_key for m in sent} == {f"approval:{aid}:handled"}


async def test_taskless_approval_decisions_close_without_a_resume(user, rec_bus, sent):
    aid = await approvals.create(user.id, None, "send_note", {"text": "x"}, "Send note: x",
                                 utcnow() + timedelta(hours=1))
    await flow.handle_approval_button(_press(user.id, f"ap:{aid}:no"))
    assert (await approvals.get(aid)).status == ApprovalStatus.REJECTED
    aid2 = await approvals.create(user.id, None, "send_note", {"text": "y"}, "Send note: y",
                                  utcnow() + timedelta(hours=1))
    await flow.handle_approval_button(_press(user.id, f"ap:{aid2}:ok"))
    assert (await approvals.get(aid2)).status == ApprovalStatus.FAILED
    assert rec_bus.jobs == []


async def test_ap_prefix_routes_through_the_single_dispatcher(user, rec_bus, sent):
    buttons.BUTTON_HANDLERS.clear()
    buttons.register_approval_buttons()
    _, aid = await _pending(user.id)
    await buttons.dispatch_button(_press(user.id, f"ap:{aid}:no"))
    assert rec_bus.jobs[0].payload["decision"] == "no"


# --- expiry and reminders with the injectable clock -----------------------------------------------


async def test_prompt_schedules_expiry_from_the_ttl_and_clock(user, rec_bus, sent, clock, monkeypatch):
    calls = []

    class Fake:
        async def wake_me(self, user_id, at, reason, **kw):
            calls.append((kw["kind"], at, kw["dedupe_key"]))

    monkeypatch.setattr(flow.timers_service, "WakeupService", Fake)
    _, aid = await _pending(user.id)
    await flow.send_approval_prompt(user.id, {"approval_id": aid})
    await flow.send_approval_prompt(user.id, {"approval_id": aid})
    by_kind = {k: (at, key) for k, at, key in calls}
    assert len(calls) == 2
    assert by_kind["system_approval_expire"][0] == clock.t + timedelta(hours=48)
    assert by_kind["system_approval_remind"][0] == clock.t + timedelta(hours=46)


async def test_wakeup_handlers_remind_and_expire(user, rec_bus, sent):
    _, aid = await _pending(user.id)
    await flow.on_remind_wakeup(user.id, f"approval:{aid}")
    await flow.on_remind_wakeup(user.id, f"approval:{aid}")
    assert [m.dedupe_key for m in sent].count(f"approval:{aid}:remind") == 2  # outbox dedupes the key
    await flow.on_expire_wakeup(user.id, f"approval:{aid}")
    assert [j.payload["decision"] for j in rec_bus.jobs] == ["expired"]


# --- sweep ----------------------------------------------------------------------------------------


async def test_sweep_expires_overdue_approvals_whose_wakeup_was_lost(user, rec_bus, sent, clock):
    _, aid = await _pending(user.id)
    assert (await flow.sweep())["expired"] == 0
    clock.advance(hours=49)
    assert (await flow.sweep())["expired"] == 1
    assert rec_bus.jobs[0].payload["decision"] == "expired"
    await flow.sweep()
    assert len(rec_bus.jobs) == 1  # now RESOLVING, not re-expired


async def test_sweep_leaves_fresh_resolving_alone_and_fails_stale_one(user, rec_bus, sent, clock):
    tid, aid = await _pending(user.id)
    await tasks.set_status(tid, TaskStatus.AWAITING_APPROVAL)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    await flow.sweep()
    assert (await approvals.get(aid)).status == ApprovalStatus.RESOLVING
    clock.advance(minutes=21)
    assert (await flow.sweep())["stuck"] == 1
    assert (await approvals.get(aid)).status == ApprovalStatus.FAILED
    assert (await tasks.get(tid)).status == TaskStatus.FAILED
    assert "lost your answer" in sent[-1].text
    assert not _DASHES.search(sent[-1].text)


async def test_sweep_stuck_resolving_of_a_cancelled_task_tells_the_user(user, rec_bus, sent, clock):
    tid, aid = await _pending(user.id)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    await tasks.cancel(user.id, tid)
    clock.advance(minutes=21)
    await flow.sweep()
    assert (await approvals.get(aid)).status == ApprovalStatus.FAILED
    assert "nothing was done" in sent[-1].text and "Send note: hi" in sent[-1].text
    n = len(sent)
    await flow.sweep()
    assert len(sent) == n


async def test_sweep_skips_resolving_of_a_running_task(user, rec_bus, sent, clock):
    tid, aid = await _pending(user.id)
    await tasks.set_status(tid, TaskStatus.RUNNING)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    clock.advance(minutes=21)
    assert (await flow.sweep())["stuck"] == 0
    assert (await approvals.get(aid)).status == ApprovalStatus.RESOLVING


async def test_sweep_reports_executed_started_unfinished_as_may_have_gone_through(user, rec_bus, sent, clock):
    tid, aid = await _pending(user.id)
    await _force(aid, status=ApprovalStatus.EXECUTED.value, started_at=utcnow())
    await flow.sweep()
    assert sent == []  # still within the task time limit
    clock.advance(minutes=30)
    assert (await flow.sweep())["may_have_run"] == 1
    assert "may have gone through" in sent[-1].text
    assert sent[-1].dedupe_key == f"approval:{aid}:may_have_run"
    ap = await approvals.get(aid)
    assert ap.status == ApprovalStatus.FAILED and ap.resolved_at is not None
    n = len(sent)
    await flow.sweep()
    assert len(sent) == n


async def test_sweep_ignores_executed_that_never_started(user, rec_bus, sent, clock):
    _, aid = await _pending(user.id)
    await _force(aid, status=ApprovalStatus.EXECUTED.value)
    clock.advance(hours=2)
    assert (await flow.sweep())["may_have_run"] == 0


async def test_sweep_reports_executed_approval_of_a_task_cancelled_while_waiting(user, rec_bus, sent, clock):
    tid, aid = await _pending(user.id)
    await _force(aid, status=ApprovalStatus.EXECUTED.value, started_at=utcnow(), resolved_at=utcnow())
    await tasks.cancel(user.id, tid)
    assert (await flow.sweep())["ran_after_stop"] == 1
    assert "went through before the task stopped" in sent[-1].text
    assert sent[-1].dedupe_key == f"approval:{aid}:ran_after_stop"
    clock.advance(days=2)
    assert (await flow.sweep())["ran_after_stop"] == 0


async def test_sweep_for_one_user_ignores_others(user, rec_bus, sent, clock):
    from mavis.store.repo import users

    other, _ = await users.get_or_create_by_chat(888, "Other")
    _, aid = await _pending(user.id)
    clock.advance(hours=49)
    assert (await flow.sweep(other.id))["expired"] == 0
    assert (await flow.sweep(user.id))["expired"] == 1


async def test_sweep_step_failure_does_not_stop_the_rest(user, rec_bus, sent, clock, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(approvals, "overdue_open", boom)
    _, aid = await _pending(user.id)
    await _force(aid, status=ApprovalStatus.EXECUTED.value, started_at=utcnow())
    clock.advance(minutes=30)
    out = await flow.sweep()
    assert out["expired"] == 0 and out["may_have_run"] == 1


def test_register_sweeps_hooks_startup_morning_and_wakeups():
    from mavis.initiative import routines
    from mavis.timers import system
    from mavis.worker import runner

    flow.register_sweeps()
    assert any(f.__name__ == "sweep_approvals" for f in runner._startup_hooks)
    assert any(f.__name__ == "sweep_for_user" for f in routines._morning_hooks)
    assert system.SYSTEM_WAKEUP_HANDLERS["system_approval_remind"] is flow.on_remind_wakeup
    assert system.SYSTEM_WAKEUP_HANDLERS["system_approval_expire"] is flow.on_expire_wakeup
    routines._morning_hooks[:] = [f for f in routines._morning_hooks if f.__name__ != "sweep_for_user"]


async def test_repeated_sweeps_log_one_history_row(user, rec_bus, clock):
    from sqlalchemy import select

    from mavis.store.models import Message

    tid, aid = await _pending(user.id)
    await _force(aid, status=ApprovalStatus.EXECUTED.value, started_at=utcnow(), resolved_at=utcnow())
    await tasks.cancel(user.id, tid)
    await flow.sweep()
    await flow.sweep()
    async with Session() as s:
        rows = list(await s.scalars(select(Message).where(Message.user_id == user.id)))
    assert [r.content for r in rows if "went through" in r.content].__len__() == 1
