"""Wire the machine into the worker when MACHINE_ENABLED (Phase 12). Idempotent."""

from __future__ import annotations

import asyncio

from mavis import machine
from mavis.agents import cancellation
from mavis.agents.context_hooks import register_context_provider
from mavis.config import get_settings
from mavis.machine import intake
from mavis.worker.runner import register_startup_hook

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
    """Account deletion: the user's sandbox files go too (S3 or local), even if the machine is now off."""
    from mavis.machine.selection import build_store

    await build_store().purge_user(user_id)
    return {"workspace": 1}


def register_machine() -> None:
    """Flags off: nothing is registered and get_runtime() stays whatever it was (None by default), except
    the deletion step, which must run whenever a user's workspace might exist."""
    from mavis.agents.specialists import register_machine_specialists, unregister_machine_specialists
    from mavis.store.repo.deletion import register_deletion_step

    register_deletion_step("workspace", _purge_workspace)

    if not get_settings().machine_enabled:
        unregister_machine_specialists()  # a test or a restart that flipped the flag sees the off state
        return
    if machine.get_runtime() is None:
        from mavis.machine.selection import build_runtime

        machine.set_runtime(build_runtime())
    from mavis.tools.machine_tools import register_machine_tools
    from mavis.tools.registry import get_registry

    register_machine_tools(get_registry())
    register_machine_specialists()
    register_context_provider(intake.inbox_context)
    cancellation.register_cancel_hook(_cancel_hook)
    register_startup_hook(_start_reaper)
