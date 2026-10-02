from datetime import datetime, timedelta

import pytest
from sqlalchemy.dialects import sqlite

from zento.domain.messages import Button, Outbound, Role
from zento.store.db import Session, UTCDateTime, utcnow
from zento.store.repo import events, messages, outbox, users


async def test_get_or_create_by_chat_is_idempotent(db) -> None:
    u1, created1 = await users.get_or_create_by_chat(42, "Jai")
    u2, created2 = await users.get_or_create_by_chat(42, "Someone else")
    assert created1 and not created2
    assert u1.id == u2.id
    assert u2.name == "Jai"
    assert u1.timezone == "Asia/Kolkata"
    assert await users.get_by_chat(42) is not None
    assert await users.get_by_chat(43) is None
    assert await users.all_ids() == [u1.id]


async def test_update_and_state(db) -> None:
    u, _ = await users.get_or_create_by_chat(1, None)
    await users.update(u.id, name="Jai", onboarded=True)
    await users.set_state(u.id, gmail_cursor="abc")
    await users.set_state(u.id, other=1)
    fresh = await users.get(u.id)
    assert fresh.name == "Jai" and fresh.onboarded
    assert fresh.state == {"gmail_cursor": "abc", "other": 1}
    assert await users.get_state(u.id) == {"gmail_cursor": "abc", "other": 1}


async def test_messages_log_recent_and_dedupe(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    assert await messages.log(u.id, Role.USER, "hi", event_id="e1")
    assert not await messages.log(u.id, Role.USER, "hi", event_id="e1")
    assert await messages.log(u.id, Role.ASSISTANT, "hey")
    rows = await messages.recent(u.id)
    assert [(r.role, r.content) for r in rows] == [("user", "hi"), ("assistant", "hey")]
    fresh = await users.get(u.id)
    assert fresh.last_user_msg_at is not None and fresh.last_user_msg_at.tzinfo is not None
    assert fresh.last_agent_msg_at is not None


async def test_recent_respects_limit_and_order(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    for i in range(5):
        await messages.log(u.id, Role.USER, f"m{i}")
    assert [r.content for r in await messages.recent(u.id, limit=3)] == ["m2", "m3", "m4"]


async def test_outbox_enqueue_dedupes(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    async with Session() as s:
        a = await outbox.enqueue(s, Outbound(user_id=u.id, text="x", dedupe_key="k"))
        b = await outbox.enqueue(s, Outbound(user_id=u.id, text="x", dedupe_key="k"))
        await s.commit()
    assert a == b
    assert len(await outbox.due(utcnow())) == 1


async def test_outbox_claim_is_exclusive_with_lease(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    oid = await outbox.enqueue_now(Outbound(user_id=u.id, text="x"))
    now = utcnow()
    assert await outbox.claim(oid, now)
    assert not await outbox.claim(oid, now)
    assert await outbox.due(now) == []
    later = now + outbox.LEASE + timedelta(seconds=1)  # a crashed sender's lease expires
    assert [r.id for r in await outbox.due(later)] == [oid]


async def test_outbox_mark_retry_sent_failed(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    oid = await outbox.enqueue_now(Outbound(user_id=u.id, text="x"))
    now = utcnow()
    await outbox.mark_retry(oid, "boom", now + timedelta(seconds=10))
    assert await outbox.due(now) == []
    [row] = await outbox.due(now + timedelta(seconds=11))
    assert row.attempts == 1 and row.last_error == "boom" and row.status == "pending"
    await outbox.mark_retry(oid, "rate", now, count_attempt=False)
    [row] = await outbox.due(now)
    assert row.attempts == 1
    await outbox.mark_sent(oid, [7, 8])
    assert await outbox.due(now + timedelta(days=1)) == []
    oid2 = await outbox.enqueue_now(Outbound(user_id=u.id, text="y"))
    await outbox.mark_failed(oid2, "dead")
    assert await outbox.due(now + timedelta(days=1)) == []


async def test_outbox_round_trip_to_outbound(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    msg = Outbound(user_id=u.id, text="Send?", buttons=[[Button(label="✅ Send", data="appr:1:yes")]],
                   proactive=True, dedupe_key="d1")
    await outbox.enqueue_now(msg)
    [row] = await outbox.due(utcnow())
    assert outbox.to_outbound(row) == msg


async def test_processed_event_claim_once(db) -> None:
    async with Session() as s:
        assert await events.claim(s, "e1")
        await s.commit()
    async with Session() as s:
        assert not await events.claim(s, "e1")


def test_utc_datetime_rejects_naive() -> None:
    with pytest.raises(ValueError):
        UTCDateTime().process_bind_param(datetime(2026, 1, 1), sqlite.dialect())
