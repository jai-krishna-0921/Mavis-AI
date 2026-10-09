"""One parametrised suite every Sandbox adapter must pass (spec 13.2).

`fake` and `local` always run; `agentcore` runs only with MAVIS_LIVE_AGENTCORE=1 and AWS credentials
(never in CI). A future E2B adapter is added to BACKENDS and must pass unchanged."""

from __future__ import annotations

import os

import pytest

BACKENDS = ["fake", "local", "agentcore"]
EXEC_BACKENDS = {"local", "agentcore"}  # the fake does not run code


@pytest.fixture(params=BACKENDS)
async def sandbox(request, settings, tmp_path):
    name = request.param
    if name == "agentcore":
        if os.environ.get("MAVIS_LIVE_AGENTCORE") != "1":
            pytest.skip("live AgentCore checks are opt-in")
        from mavis.machine.agentcore import AgentCoreSandbox

        sb = AgentCoreSandbox()
    elif name == "local":
        from mavis.machine.local import LocalSandbox

        sb = LocalSandbox(root=tmp_path / "local")
    else:
        from mavis.machine.fake import FakeSandbox

        sb = FakeSandbox()
    sb.contract_name = name
    yield sb


@pytest.fixture
def needs_exec(sandbox):
    if sandbox.contract_name not in EXEC_BACKENDS:
        pytest.skip("this backend does not execute code")
