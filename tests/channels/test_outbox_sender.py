from datetime import timedelta

from mavis.channels.base import ChannelRateLimited
from mavis.channels.outbox_sender import MAX_ATTEMPTS, OutboxSender
from mavis.domain.messages import Button, Outbound
from mavis.store.db import Session, utcnow
from mavis.store.models import OutboxMessage
from mavis.store.repo import outbox, users


async def _user() -> int:
    u, _ = await users.get_or_create_by_chat(555, "Jai")
    return u.id


async def _row(outbox_id: int) -> OutboxMessage:
    async with Session() as s:
        return await s.get_one(OutboxMessage, outbox_id)


async def test_delivers_text_with_buttons_and_marks_sent(db, channel) -> None:
    uid = await _user()
    oid = await outbox.enqueue_now(Outbound(user_id=uid, text="Send it?",
                                            buttons=[[Button(label="✅", data="a:1:y")]]))
    assert await OutboxSender(channel).run_once() == 1
    assert channel.texts == ["Send it?"]
    assert channel.sent[0].chat_id == 555 and channel.sent[0].buttons[0][0].data == "a:1:y"
    row = await _row(oid)
    assert row.status == "sent" and row.provider_message_ids == [1]
    assert await OutboxSender(channel).run_once() == 0  # nothing left


async def test_deliver_pending_helper(db, channel) -> None:
    from mavis.channels.outbox_sender import deliver_pending

    uid = await _user()
    await outbox.enqueue_now(Outbound(user_id=uid, text="one"))
    await outbox.enqueue_now(Outbound(user_id=uid, text="two"))
    assert await deliver_pending(channel) == 2
    assert channel.texts == ["one", "two"]


async def test_delivers_document_with_caption(db, channel, settings) -> None:
    from mavis.store import artifacts

    uid = await _user()
    artifacts.user_dir(uid).mkdir(parents=True, exist_ok=True)
    f = artifacts.user_dir(uid) / "deck.pptx"
    f.write_bytes(b"x")
    await outbox.enqueue_now(Outbound(user_id=uid, text="Your deck", document_path=str(f)))
    await OutboxSender(channel).run_once()
    assert channel.sent[0].kind == "document" and channel.sent[0].path == str(f)


async def test_failure_backs_off_then_succeeds(db, channel) -> None:
    uid = await _user()
    oid = await outbox.enqueue_now(Outbound(user_id=uid, text="hi"))
    channel.fail_next.append(RuntimeError("network"))
    now = utcnow()
    assert await OutboxSender(channel).run_once(now) == 0
    row = await _row(oid)
    assert row.status == "pending" and row.attempts == 1
    assert row.next_attempt_at >= now + timedelta(seconds=2) - timedelta(milliseconds=5)
    assert await OutboxSender(channel).run_once(now) == 0  # not due yet
    assert await OutboxSender(channel).run_once(now + timedelta(seconds=3)) == 1
    assert channel.texts == ["hi"]


async def test_rate_limit_defers_without_counting_attempt(db, channel) -> None:
    uid = await _user()
    oid = await outbox.enqueue_now(Outbound(user_id=uid, text="hi"))
    channel.fail_next.append(ChannelRateLimited(7))
    now = utcnow()
    await OutboxSender(channel).run_once(now)
    row = await _row(oid)
    assert row.attempts == 0 and row.status == "pending"
    assert abs((row.next_attempt_at - (now + timedelta(seconds=7))).total_seconds()) < 1


async def test_gives_up_after_max_attempts(db, channel) -> None:
    uid = await _user()
    oid = await outbox.enqueue_now(Outbound(user_id=uid, text="hi"))
    now = utcnow()
    for i in range(MAX_ATTEMPTS):
        channel.fail_next.append(RuntimeError("down"))
        await OutboxSender(channel).run_once(now + timedelta(hours=i))
    row = await _row(oid)
    assert row.status == "failed" and channel.texts == []


async def test_deduped_messages_sent_once(db, channel) -> None:
    uid = await _user()
    await outbox.enqueue_now(Outbound(user_id=uid, text="hi", dedupe_key="reply:e1:0"))
    await outbox.enqueue_now(Outbound(user_id=uid, text="hi", dedupe_key="reply:e1:0"))
    await OutboxSender(channel).run_once()
    assert channel.texts == ["hi"]


async def test_later_row_waits_for_earlier_retrying_row_of_same_user(db, channel) -> None:
    uid = await _user()
    other, _ = await users.get_or_create_by_chat(556, "Sam")
    r1 = await outbox.enqueue_now(Outbound(user_id=uid, text="one"))
    await outbox.enqueue_now(Outbound(user_id=uid, text="two"))
    await outbox.enqueue_now(Outbound(user_id=other.id, text="sam-1"))
    channel.fail_next.append(ChannelRateLimited(30))
    now = utcnow()
    sender = OutboxSender(channel)
    await sender.run_once(now)
    assert channel.texts == ["sam-1"]  # row1 rate-limited; row2 held back; other user unaffected
    await sender.run_once(now + timedelta(seconds=10))
    assert channel.texts == ["sam-1"]  # row1 still in backoff
    assert await sender.run_once(now + timedelta(seconds=31)) == 2
    assert channel.texts == ["sam-1", "one", "two"]
    assert (await _row(r1)).status == "sent"
