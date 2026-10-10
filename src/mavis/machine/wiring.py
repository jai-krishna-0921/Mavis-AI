"""Wire the machine into the worker when MACHINE_ENABLED (Phase 12). Idempotent."""

from __future__ import annotations

import asyncio

import structlog

from mavis import machine
from mavis.agents import cancellation
from mavis.agents.context_hooks import register_context_provider
from mavis.config import get_settings
from mavis.machine import intake
from mavis.worker.runner import register_startup_hook

log = structlog.get_logger(__name__)
_reaper: asyncio.Task | None = None


async def _cancel_hook(task_id: int) -> None:
    if (rt := machine.get_runtime()) is not None:
        await rt.cancel(task_id)


async def _start_reaper() -> None:
    global _reaper
    rt = machine.get_runtime()
    if rt is not None and (_reaper is None or _reaper.done()):
        _reaper = asyncio.create_task(rt.run_reaper_forever())


async def _purge_workspace(user_id: int) -> dict:
    """Account deletion: the user's sandbox files go too (S3 or local), even if the machine is now off. A
    user who never had a session or a file has nothing there, and the store is not called: with the
    machine off the box has no S3 credentials, and every deletion failed on this step (evals 2026-10-10)."""
    from mavis.machine.selection import build_store
    from mavis.store.repo import machine as repo_machine

    if not await repo_machine.ever_used(user_id):
        return {"workspace": 0}
    await build_store().purge_user(user_id)
    return {"workspace": 1}


def register_machine() -> None:
    """Flags off: nothing is registered and get_runtime() stays whatever it was (None by default), except
    the deletion step, which must run whenever a user's workspace might exist."""
    from mavis.agents.specialists import unregister_machine_specialists
    from mavis.store.repo.deletion import register_deletion_step

    register_deletion_step("workspace", _purge_workspace)

    if not get_settings().machine_enabled:
        unregister_machine_specialists()  # a test or a restart that flipped the flag sees the off state
        return
    if machine.get_runtime() is None:
        from mavis.machine.selection import build_runtime

        machine.set_runtime(build_runtime())
    s = get_settings()
    if s.env == "prod" and not s.machine_allow_egress:
        # tools appear only after a probe from inside a session shows the interpreter has no network
        register_startup_hook(_gate_on_isolation)
        return
    _activate()


def _activate() -> None:
    from mavis.agents.specialists import register_machine_specialists
    from mavis.tools.machine_tools import register_machine_tools
    from mavis.tools.registry import get_registry

    register_machine_tools(get_registry())
    register_machine_specialists()
    register_context_provider(intake.inbox_context)
    cancellation.register_cancel_hook(_cancel_hook)
    register_startup_hook(_start_reaper)


async def _gate_on_isolation() -> None:
    from mavis.machine.isolation import check_isolation

    rt = machine.get_runtime()
    if rt is None:
        return
    result = await check_isolation(rt.sandbox)
    if not result.isolated:
        log.error("machine.disabled_egress", detail=result.detail,
                  hint="use a SANDBOX custom interpreter (deploy/aws/machine.sh --custom-interpreter) "
                       "or set MACHINE_ALLOW_EGRESS=true to accept network access")
        machine.set_runtime(None)  # uploads and chat see the machine as off
        return
    _activate()
    await _start_reaper()
