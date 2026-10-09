"""Pick adapters from settings. Task 15 adds the AgentCore sandbox and the prod refusal details."""

from __future__ import annotations

from mavis.config import get_settings
from mavis.machine.runtime import MachineRuntime


def build_sandbox():
    s = get_settings()
    choice = s.sandbox_backend
    if choice == "fake":
        from mavis.machine.fake import FakeSandbox

        return FakeSandbox()
    if choice == "local" or (choice == "auto" and s.env != "prod"):
        from mavis.machine.local import LocalSandbox

        return LocalSandbox()
    raise RuntimeError(f"SANDBOX_BACKEND={choice} is not available in this build")


def build_store():
    s = get_settings()
    if s.workspace_backend == "s3" or (s.workspace_backend == "auto" and s.workspace_bucket):
        from mavis.machine.s3store import S3WorkspaceStore

        return S3WorkspaceStore()
    from mavis.machine.local import LocalWorkspaceStore

    return LocalWorkspaceStore()


def build_runtime() -> MachineRuntime:
    return MachineRuntime(build_sandbox(), build_store())
