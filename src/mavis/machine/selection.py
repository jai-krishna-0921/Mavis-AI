"""Pick adapters from settings. `auto` means AgentCore when AWS credentials resolve (the instance role in
prod), the local sandbox in dev, and a refusal in prod without credentials: local is never a prod fallback."""

from __future__ import annotations

from mavis.config import get_settings
from mavis.machine.runtime import MachineRuntime


def _aws_credentials() -> bool:
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001 - missing botocore config or a broken provider chain
        return False


def build_sandbox():
    s = get_settings()
    choice = s.sandbox_backend
    if choice == "fake":
        from mavis.machine.fake import FakeSandbox

        return FakeSandbox()
    if choice == "e2b":
        raise RuntimeError("SANDBOX_BACKEND=e2b is not built yet")
    if choice == "agentcore" or (choice == "auto" and _aws_credentials()):
        from mavis.machine.agentcore import AgentCoreSandbox

        return AgentCoreSandbox()
    if s.env != "prod" and choice in ("local", "auto"):
        from mavis.machine.local import LocalSandbox

        return LocalSandbox()
    if choice == "local":
        raise RuntimeError("SANDBOX_BACKEND=local runs code on the host and is refused in prod")
    raise RuntimeError("no AWS credentials for AgentCore in prod: attach the mavis-ec2 role")


def build_store():
    s = get_settings()
    if s.workspace_backend == "s3" or (s.workspace_backend == "auto" and s.workspace_bucket):
        from mavis.machine.s3store import S3WorkspaceStore

        return S3WorkspaceStore()
    from mavis.machine.local import LocalWorkspaceStore

    return LocalWorkspaceStore()


def build_runtime() -> MachineRuntime:
    return MachineRuntime(build_sandbox(), build_store())
