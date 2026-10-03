from mavis.domain.tasks import TaskKind, TaskOrigin, TaskStatus
from mavis.store.repo import tasks


async def test_create_and_get_task(user):
    tid = await tasks.create(user.id, goal="compare laptops", context="budget 1L", origin=TaskOrigin.USER)
    t = await tasks.get(tid)
    assert t is not None
    assert t.goal == "compare laptops"
    assert t.status == TaskStatus.QUEUED
    assert t.kind == TaskKind.TASK


async def test_task_claim_is_atomic(user):
    tid = await tasks.create(user.id, goal="x")
    assert await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.RUNNING) is True
    assert await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.RUNNING) is False
    assert await tasks.running_count(user.id) == 1


async def test_next_queued_is_oldest_first(user):
    first = await tasks.create(user.id, goal="a")
    await tasks.create(user.id, goal="b")
    nxt = await tasks.next_queued(user.id)
    assert nxt is not None and nxt.id == first


async def test_cancel_only_own_active_task(user):
    from mavis.store.repo import users

    other, _ = await users.get_or_create_by_chat(9999, "Other")
    tid = await tasks.create(user.id, goal="a")
    assert await tasks.cancel(other.id, tid) is False
    assert await tasks.cancel(user.id, tid) is True
    assert (await tasks.get(tid)).status == TaskStatus.CANCELLED
    assert await tasks.cancel(user.id, tid) is False


async def test_artifacts_round_trip(user):
    tid = await tasks.create(user.id, goal="deck")
    await tasks.add_artifact(
        tid, user.id, kind="pptx", path="/tmp/deck.pptx", mime="application/vnd.ms-powerpoint"
    )
    arts = await tasks.artifacts_for(tid)
    assert [a.path for a in arts] == ["/tmp/deck.pptx"]


async def test_set_status_done_sets_finished_at(user):
    tid = await tasks.create(user.id, goal="a")
    await tasks.set_status(tid, TaskStatus.DONE, result_text="ok")
    t = await tasks.get(tid)
    assert t.finished_at is not None and t.result_text == "ok"


async def test_tainted_flag_persists(user):
    assert (await tasks.get(await tasks.create(user.id, goal="a"))).tainted is False
    assert (await tasks.get(await tasks.create(user.id, goal="b", tainted=True))).tainted is True


async def test_terminal_claim_never_overwrites_cancel(user):
    tid = await tasks.create(user.id, goal="a")
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert await tasks.cancel(user.id, tid)
    active = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL)
    assert await tasks.claim(tid, active, TaskStatus.DONE, result_text="late") is False
    t = await tasks.get(tid)
    assert t.status == TaskStatus.CANCELLED and t.result_text is None


async def test_terminal_claim_writes_fields(user):
    tid = await tasks.create(user.id, goal="a")
    assert await tasks.claim(tid, [TaskStatus.QUEUED, TaskStatus.RUNNING], TaskStatus.DONE, result_text="ok")
    t = await tasks.get(tid)
    assert t.status == TaskStatus.DONE and t.result_text == "ok" and t.finished_at is not None


async def test_save_plan_skips_finished_tasks(user):
    tid = await tasks.create(user.id, goal="a")
    assert await tasks.save_plan(tid, {"steps": []}) is True
    await tasks.cancel(user.id, tid)
    assert await tasks.save_plan(tid, {"steps": [1]}) is False
    assert (await tasks.get(tid)).plan == {"steps": []}
