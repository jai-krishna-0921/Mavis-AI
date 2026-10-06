"""attention_observations.source (Workspace spec 6): Workspace signals share the table, mail readers don't
see them, and the unique (user, message_id) key dedupes signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mavis.domain import timeutil
from mavis.store.db import Session
from mavis.store.models import AttentionSender
from mavis.store.repo import attention as repo

NOW = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)


async def _signal(user_id: int, mid: str, kind: str = "file_shared", source: str = "drive"):
    return await repo.insert_signal(
        user_id, mid, source=source, kind=kind, verdict="brief", urgency=0, summary="Q3 deck",
        facts={"actor": "priya@example.com"}, received_at=NOW,
    )


async def test_insert_signal_is_idempotent(user):
    first, created = await _signal(user.id, "drive:f1:shared-2026-10-03T03:00:00")
    again, created_again = await _signal(user.id, "drive:f1:shared-2026-10-03T03:00:00")
    assert created and not created_again and first.id == again.id
    assert first.status == repo.DONE and first.source == "drive" and first.verdict == "brief"


async def test_recent_is_mail_only_by_default(user):
    mail, _ = await repo.insert_pending(user.id, "m1", thread_id="", origin=repo.ORIGIN_LIVE,
                                        sender_domain="x.in", sender_name="", received_at=NOW, payload={})
    await repo.finish(mail.id, verdict="brief", summary="Invoice")
    await _signal(user.id, "drive:f1:e1")
    assert [r.message_id for r in await repo.recent(user.id, NOW - timedelta(hours=1))] == ["m1"]
    everything = await repo.recent(user.id, NOW - timedelta(hours=1), source=None)
    assert {r.message_id for r in everything} == {"m1", "drive:f1:e1"}
    assert mail.source == "mail"


async def test_signals_filter_by_source_and_kind(user):
    await _signal(user.id, "drive:f1:e1")
    await _signal(user.id, "tasks:t1:task_due-2026-10-03", kind="task_due", source="tasks")
    due = await repo.signals(user.id, NOW - timedelta(hours=1), sources=("tasks",), kinds=("task_due",))
    assert [r.message_id for r in due] == ["tasks:t1:task_due-2026-10-03"]
    assert len(await repo.signals(user.id, NOW - timedelta(hours=1), sources=("drive", "tasks"))) == 2


async def test_sender_known(user):
    async with Session() as s:
        s.add(AttentionSender(user_id=user.id, address="priya@example.com", domain="example.com", count=3,
                              first_seen=NOW, last_seen=NOW))
        await s.commit()
    assert await repo.sender_known(user.id, "Priya@Example.com")
    assert not await repo.sender_known(user.id, "stranger@example.com")


async def test_has_any_counts_only_mail(user):
    await _signal(user.id, "drive:f1:e1")
    assert not await repo.has_any(user.id)
    mail, _ = await repo.insert_pending(user.id, "m1", thread_id="", origin=repo.ORIGIN_LIVE,
                                        sender_domain="x.in", sender_name="", received_at=NOW, payload={})
    assert mail.source == "mail"
    assert await repo.has_any(user.id)



async def _mail(user_id: int, mid: str, **fields):
    row, _ = await repo.insert_pending(user_id, mid, thread_id="", origin=repo.ORIGIN_LIVE,
                                       sender_domain="x.in", sender_name="", received_at=NOW, payload={})
    await repo.finish(row.id, verdict="brief", summary="Mail", **fields)
    return row


async def test_mail_redelivery_and_drain_never_pick_up_workspace_rows(user):
    ws, _ = await _signal(user.id, "drive:f1:e1")
    await repo.set_fields(ws.id, delivery=repo.QUEUED)
    later = timeutil.now() + timedelta(days=1)
    assert await repo.undelivered(user.id, later) == []
    assert await repo.users_needing_drain() == []
    mail = await _mail(user.id, "m1", delivery=repo.QUEUED)
    assert [r.id for r in await repo.undelivered(user.id, later)] == [mail.id]
    assert await repo.users_needing_drain() == [user.id]


async def test_money_and_security_readers_ignore_workspace_rows(user):
    debit = {"money": {"direction": "debit", "counterparty_key": "acme"}, "baselined": True,
             "authenticated": True}
    ws, _ = await _signal(user.id, "drive:f1:e1")
    await repo.set_fields(ws.id, facts=debit, kind="security", sender_domain="x.in")
    since = NOW - timedelta(hours=1)
    assert await repo.recent_debits(user.id, since, exclude_id=0) == 0
    assert not await repo.prior_security(user.id, "x.in", exclude_id=0)
    assert await repo.baselined_on(user.id, "acme", since, NOW + timedelta(hours=1)) == 0
    assert await repo.by_ids(user.id, [ws.id]) == []
    mail = await _mail(user.id, "m1", facts=debit, kind="security")
    assert await repo.recent_debits(user.id, since, exclude_id=0) == 1
    assert await repo.prior_security(user.id, "x.in", exclude_id=0)
    assert await repo.baselined_on(user.id, "acme", since, NOW + timedelta(hours=1)) == 1
    assert [r.id for r in await repo.by_ids(user.id, [ws.id, mail.id])] == [mail.id]
