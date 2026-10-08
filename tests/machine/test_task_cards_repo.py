from __future__ import annotations

from mavis.store.repo import task_cards, tasks


async def _task(user, goal="compare three kettles"):
    return await tasks.create(user.id, goal=goal)


async def test_save_get_and_upsert(db, user):
    tid = await _task(user)
    assert await task_cards.get(tid) is None
    await task_cards.save(tid, user.id, chat_id=8080, message_id=None, state={"v": 1}, final=False)
    await task_cards.set_message(tid, 412)
    await task_cards.save(tid, user.id, chat_id=8080, message_id=412, state={"v": 2}, final=True)
    row = await task_cards.get(tid)
    assert (row.message_id, row.state, row.final) == (412, {"v": 2}, True)


async def test_mark_delivered_is_once(db, user):
    tid = await _task(user, "plot rainfall")
    a = await tasks.add_artifact(tid, user.id, kind="png", path="/tmp/rain.png", mime="image/png")
    b = await tasks.add_artifact(tid, user.id, kind="csv", path="/tmp/rain.csv", mime="text/csv")
    assert await tasks.mark_delivered(a) is True
    assert await tasks.mark_delivered(a) is False
    assert [x.id for x in await tasks.undelivered_artifacts(tid)] == [b]
