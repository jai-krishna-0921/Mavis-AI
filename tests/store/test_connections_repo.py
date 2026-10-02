from datetime import UTC, datetime

from mavis.domain.integrations import PendingStatus, user_from_provider_id
from mavis.domain.policy import Capability
from mavis.store.repo import connections, users

NOW = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)


async def test_pending_lifecycle(db):
    pid = await connections.create_pending(1, Capability.GMAIL, "check your email", "task-1", now=NOW)
    row = await connections.get_pending(pid)
    assert row is not None and row.status == PendingStatus.PENDING and row.task_id == "task-1"
    assert [p.id for p in await connections.open_for(1, Capability.GMAIL)] == [pid]
    assert (await connections.latest_open(1, Capability.GMAIL)).id == pid
    await connections.resolve(pid, PendingStatus.ACTIVE, now=NOW)
    assert await connections.open_for(1, Capability.GMAIL) == []
    assert (await connections.get_pending(pid)).status == PendingStatus.ACTIVE


async def test_open_for_is_scoped_by_user_and_capability(db):
    await connections.create_pending(1, Capability.GMAIL, "", None, now=NOW)
    await connections.create_pending(2, Capability.GMAIL, "", None, now=NOW)
    await connections.create_pending(1, Capability.SLACK, "", None, now=NOW)
    assert len(await connections.open_for(1, Capability.GMAIL)) == 1


def test_user_from_provider_id():
    assert user_from_provider_id("mavis-7") == 7
    assert user_from_provider_id("someone-else") is None
    assert user_from_provider_id(None) is None


async def test_user_state_shallow_merge(db):
    user, _ = await users.get_or_create_by_chat(4242, "Jai")
    await users.update_state(user.id, {"a": 1})
    merged = await users.update_state(user.id, {"b": {"x": 2}})
    assert merged == {"a": 1, "b": {"x": 2}}
    assert await users.get_state(user.id) == {"a": 1, "b": {"x": 2}}
