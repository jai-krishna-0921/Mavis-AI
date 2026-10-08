from __future__ import annotations

from mavis.channels.fake import FakeChannel
from mavis.channels.outbox_sender import OutboxSender
from mavis.domain.messages import Outbound
from mavis.store.repo import outbox, users


async def _user(chat):
    u, _ = await users.get_or_create_by_chat(chat, "Tess")
    return u


async def test_photo_row_is_sent_as_a_photo(db):
    u = await _user(6001)
    await outbox.enqueue_now(Outbound(user_id=u.id, text="Screenshot of tea.example", photo_path="/tmp/t.png",
                                      dedupe_key="task:1:shot:1"))
    ch = FakeChannel()
    assert await OutboxSender(ch).run_once() == 1
    assert ch.photos == [(6001, "/tmp/t.png", "Screenshot of tea.example")]


async def test_album_row_is_sent_as_a_media_group(db):
    u = await _user(6002)
    await outbox.enqueue_now(Outbound(user_id=u.id, text="step 1\nstep 2\nstep 3",
                                      media=["/a.png", "/b.png", "/c.png"], dedupe_key="task:2:album"))
    ch = FakeChannel()
    await OutboxSender(ch).run_once()
    assert ch.albums == [(6002, ["/a.png", "/b.png", "/c.png"], ["step 1", "step 2", "step 3"])]


async def test_paced_rows_are_not_claimed_this_pass(db, monkeypatch):
    from mavis.channels import pacing

    class Busy(pacing.SendPacer):
        async def reserve(self, chat_id=None, *, kind="chat"):
            return 0.5

    pacing.set_pacer(Busy(rate=1))
    u = await _user(6003)
    await outbox.enqueue_now(Outbound(user_id=u.id, text="hello"))
    ch = FakeChannel()
    assert await OutboxSender(ch).run_once() == 0
    assert ch.texts == []
    from mavis.store.db import utcnow

    [row] = await outbox.due(utcnow(), 5)
    assert row.attempts == 0
