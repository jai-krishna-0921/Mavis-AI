from datetime import timedelta

from mavis.domain.tasks import ApprovalStatus
from mavis.store.db import utcnow
from mavis.store.repo import approvals, audit, policy_rules, tasks


async def _approval(user_id: int, task_id: int | None = None, preview: str = "Send note: hi") -> int:
    return await approvals.create(
        user_id=user_id, task_id=task_id, tool="send_note", arguments={"text": "hi"},
        preview=preview, expires_at=utcnow() + timedelta(hours=48),
    )


async def test_approval_claim_only_once(user):
    aid = await _approval(user.id)
    ok1 = await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    ok2 = await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    assert (ok1, ok2) == (True, False)


async def test_next_open_returns_lowest_open_id(user):
    tid = await tasks.create(user.id, goal="g")
    a1 = await _approval(user.id, tid)
    a2 = await _approval(user.id, tid)
    await approvals.set_status(a1, ApprovalStatus.EXECUTED, result="done")
    nxt = await approvals.next_open(tid)
    assert nxt is not None and nxt.id == a2


async def test_update_args_resets_to_pending(user):
    aid = await _approval(user.id)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    await approvals.update_args(aid, {"text": "hello"}, "Send note: hello")
    a = await approvals.get(aid)
    assert a.status == ApprovalStatus.PENDING
    assert a.arguments == {"text": "hello"}
    assert a.preview == "Send note: hello"


async def test_mark_prompted_once(user):
    aid = await _approval(user.id)
    assert await approvals.mark_prompted(aid) is True
    assert await approvals.mark_prompted(aid) is False


async def test_unattached_and_attach(user):
    aid = await _approval(user.id)
    assert [a.id for a in await approvals.unattached_for_user(user.id)] == [aid]
    tid = await tasks.create(user.id, goal="g")
    await approvals.attach([aid], tid)
    assert await approvals.unattached_for_user(user.id) == []
    assert (await approvals.get(aid)).task_id == tid


async def test_reject_open_for_task(user):
    tid = await tasks.create(user.id, goal="g")
    await _approval(user.id, tid)
    assert await approvals.reject_open_for_task(tid) == 1
    assert await approvals.next_open(tid) is None


async def test_policy_rule_matches_case_insensitive_substring(user):
    await policy_rules.add(user.id, tool="calendar_create_event", field="attendees",
                           contains="jawahar", description="always OK invites to Jawahar")
    assert await policy_rules.matches(user.id, "calendar_create_event", {"attendees": ["Jawahar@x.com"]})
    assert not await policy_rules.matches(user.id, "calendar_create_event", {"attendees": ["bob@x.com"]})
    assert not await policy_rules.matches(user.id, "mail_send", {"attendees": ["jawahar"]})


async def test_policy_rule_matches_non_ascii(user):
    await policy_rules.add(user.id, tool="send_note", field="text", contains="Jos\u00e9", description="ok")
    assert await policy_rules.matches(user.id, "send_note", {"text": "hola JOS\u00c9"})


async def test_audit_record(user):
    await audit.record(user.id, actor="user", action="approval.ok", detail={"approval_id": 1})
    rows = await audit.recent(user.id, limit=5)
    assert rows[0].action == "approval.ok"


async def test_set_status_never_reopens_a_resolved_approval(user):
    aid = await _approval(user.id)
    assert await approvals.set_status(aid, ApprovalStatus.REJECTED) is True
    assert await approvals.set_status(aid, ApprovalStatus.EXPIRED) is False
    assert await approvals.set_status(aid, ApprovalStatus.PENDING) is False
    assert (await approvals.get(aid)).status == ApprovalStatus.REJECTED


async def test_set_status_from_claimed_state_records_outcome(user):
    aid = await _approval(user.id)
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.EXECUTED)
    ok = await approvals.set_status(aid, ApprovalStatus.FAILED, result="boom",
                                    from_statuses={ApprovalStatus.EXECUTED})
    a = await approvals.get(aid)
    assert ok and a.status == ApprovalStatus.FAILED and a.result == "boom" and a.resolved_at is not None


async def test_update_args_ignores_resolved_approval(user):
    aid = await _approval(user.id)
    await approvals.set_status(aid, ApprovalStatus.EXECUTED, result="done")
    assert await approvals.update_args(aid, {"text": "new"}, "Send note: new") is False
    a = await approvals.get(aid)
    assert a.status == ApprovalStatus.EXECUTED and a.arguments == {"text": "hi"}


async def test_reject_open_for_task_leaves_resolving_alone(user):
    tid = await tasks.create(user.id, goal="g")
    pending = await _approval(user.id, tid)
    resolving = await _approval(user.id, tid, preview="Send note: other")
    await approvals.claim(resolving, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    assert await approvals.reject_open_for_task(tid) == 1
    assert (await approvals.get(pending)).status == ApprovalStatus.REJECTED
    assert (await approvals.get(resolving)).status == ApprovalStatus.RESOLVING


async def test_attach_skips_resolved_approvals(user):
    open_id = await _approval(user.id)
    done_id = await _approval(user.id, preview="Send note: done")
    await approvals.set_status(done_id, ApprovalStatus.REJECTED)
    tid = await tasks.create(user.id, goal="g")
    await approvals.attach([open_id, done_id], tid)
    assert (await approvals.get(open_id)).task_id == tid
    assert (await approvals.get(done_id)).task_id is None


async def test_mark_started_only_for_claimed_unstarted_rows(user):
    aid = await _approval(user.id)
    assert await approvals.mark_started(aid) is False  # still PENDING
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.EXECUTED)
    assert await approvals.mark_started(aid) is True
    assert await approvals.mark_started(aid) is False
    assert (await approvals.get(aid)).started_at is not None
