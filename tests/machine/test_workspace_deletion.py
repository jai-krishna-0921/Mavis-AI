"""Account deletion removes the user's sandbox workspace, whether or not the machine is enabled now."""

import pytest

from mavis.machine import wiring
from mavis.store.repo import deletion


@pytest.mark.parametrize("enabled", [False, True])
async def test_deletion_step_purges_the_workspace(settings, monkeypatch, enabled):
    purged: list[int] = []

    class Store:
        async def purge_user(self, user_id):
            purged.append(user_id)

    monkeypatch.setattr(settings, "machine_enabled", enabled)
    monkeypatch.setattr("mavis.machine.selection.build_store", lambda: Store())
    monkeypatch.setattr(deletion, "EXTERNAL_STEPS", {})
    monkeypatch.setattr("mavis.store.repo.machine.ever_used", _used({7}))
    if enabled:
        monkeypatch.setattr("mavis.machine.get_runtime", lambda: object())  # do not build a real runtime
        monkeypatch.setattr("mavis.tools.machine_tools.register_machine_tools", lambda reg: None)
    wiring.register_machine()
    assert "workspace" in deletion.EXTERNAL_STEPS
    assert await deletion.EXTERNAL_STEPS["workspace"](7) == {"workspace": 1}
    assert purged == [7]


def _used(ids: set[int]):
    async def ever_used(user_id: int) -> bool:
        return user_id in ids
    return ever_used


async def test_a_user_who_never_had_a_workspace_is_not_sent_to_the_store(settings, monkeypatch):
    """With the machine off the box has no S3 credentials: calling the store for a user who never had a
    session failed every deletion and reset (evals 2026-10-10)."""
    class Store:
        async def purge_user(self, user_id):
            raise AssertionError("the store must not be called")

    monkeypatch.setattr(settings, "machine_enabled", False)
    monkeypatch.setattr("mavis.machine.selection.build_store", lambda: Store())
    monkeypatch.setattr(deletion, "EXTERNAL_STEPS", {})
    monkeypatch.setattr("mavis.store.repo.machine.ever_used", _used(set()))
    wiring.register_machine()
    assert await deletion.EXTERNAL_STEPS["workspace"](8) == {"workspace": 0}


async def test_ever_used_sees_sessions_and_deleted_files(db):
    from mavis.store.repo import machine as repo_machine
    from mavis.store.repo import users

    u, _ = await users.get_or_create_by_chat(8181, "Ana")
    assert await repo_machine.ever_used(u.id) is False
    await repo_machine.reserve_session(user_id=u.id, task_id=1, kind="python", backend="local",
                                       deadline_at=None, max_open=1)
    assert await repo_machine.ever_used(u.id) is True
