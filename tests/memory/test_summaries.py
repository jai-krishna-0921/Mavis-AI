from sqlalchemy import select

from mavis.domain.messages import Role
from mavis.memory.summaries import maybe_summarize
from mavis.store import db as dbm
from mavis.store.models import Message
from mavis.store.repo import messages, summaries, users


async def make_user_with_messages(n: int) -> tuple[int, list[int]]:
    user, _ = await users.get_or_create_by_chat(111, "Jai")
    for i in range(n):
        await messages.log(user.id, Role.USER if i % 2 == 0 else Role.ASSISTANT, f"message {i}")
    async with dbm.Session() as s:
        ids = list(await s.scalars(select(Message.id).where(Message.user_id == user.id).order_by(Message.id)))
    return user.id, ids


async def test_no_summary_until_enough_messages_fall_out_of_window(db, fake_llm):
    uid, _ = await make_user_with_messages(39)  # 19 outside the 20-message window
    assert await maybe_summarize(uid) is False
    assert await summaries.latest(uid) is None


async def test_summarises_messages_outside_window_once(db, fake_llm):
    uid, ids = await make_user_with_messages(45)  # 25 outside window
    fake_llm.push_text("Jai and Mavis talked about interview prep.")
    assert await maybe_summarize(uid) is True
    latest = await summaries.latest(uid)
    assert latest.summary == "Jai and Mavis talked about interview prep."
    assert latest.upto_message_id == ids[24]
    assert await maybe_summarize(uid) is False  # nothing new outside the window


async def test_llm_failure_does_not_raise(db, monkeypatch):
    from mavis.llm import models

    uid, _ = await make_user_with_messages(45)

    class Broken:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("model down")

    monkeypatch.setattr(models, "chat_model", lambda *a, **k: Broken())
    assert await maybe_summarize(uid) is False
