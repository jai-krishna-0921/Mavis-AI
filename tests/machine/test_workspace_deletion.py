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
    if enabled:
        monkeypatch.setattr("mavis.machine.get_runtime", lambda: object())  # do not build a real runtime
        monkeypatch.setattr("mavis.tools.machine_tools.register_machine_tools", lambda reg: None)
    wiring.register_machine()
    assert "workspace" in deletion.EXTERNAL_STEPS
    assert await deletion.EXTERNAL_STEPS["workspace"](7) == {"workspace": 1}
    assert purged == [7]
