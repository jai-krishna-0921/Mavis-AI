"""One cancel path for every trigger (Cancel button, cancel_task tool, chat "stop that").

`cancel_by_user` claims the status, closes the task's approvals, sets the cancel flag (in-process and a
Redis key, so another worker's loop sees it) and runs the registered hooks (MachineRuntime stops the
task's sessions). Loops poll `is_cancelled` through react_loop(should_stop) before each model call and
tool round. Nothing cancels asyncio tasks: the step ends with TaskCancelled and `finish` loses its claim."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from mavis.bus import get_redis
from mavis.domain.errors import MavisError

log = structlog.get_logger(__name__)

CancelHook = Callable[[int], Awaitable[None]]
_hooks: list[CancelHook] = []
_local: set[int] = set()
_KEY = "mavis:cancel:task:{}"
_TTL_S = 86_400


class TaskCancelled(MavisError):
    """The user cancelled the task this loop belongs to."""


def register_cancel_hook(fn: CancelHook) -> None:
    if fn not in _hooks:
        _hooks.append(fn)


def reset_for_tests() -> None:
    _hooks.clear()
    _local.clear()


def forget(task_id: int) -> None:
    _local.discard(task_id)


async def cancel(task_id: int) -> None:
    _local.add(task_id)
    client = get_redis()
    if client is not None:
        try:
            await client.set(_KEY.format(task_id), "1", ex=_TTL_S)
        except Exception as exc:  # noqa: BLE001 - the in-process flag still stops this worker
            log.warning("cancel.redis_failed", error=type(exc).__name__)
    for fn in list(_hooks):
        try:
            await fn(task_id)
        except Exception as exc:  # noqa: BLE001 - one hook must not keep the others from running
            log.warning("cancel.hook_failed", hook=getattr(fn, "__name__", "?"), error=type(exc).__name__)


async def is_cancelled(task_id: int) -> bool:
    if task_id in _local:
        return True
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(await client.exists(_KEY.format(task_id)))
    except Exception:  # noqa: BLE001
        return False


def stop_check_for_current_task() -> Callable[[], Awaitable[bool]] | None:
    """react_loop(should_stop=...) for the task this loop runs in; None outside a task (a chat turn)."""
    from mavis.tools.registry import current_task_id  # lazy: the registry imports the store

    task_id = current_task_id.get()
    if task_id is None:
        return None

    async def should_stop() -> bool:
        return await is_cancelled(task_id)

    return should_stop


async def cancel_by_user(user_id: int, task_id: int) -> bool:
    """The one cancel path (button, cancel_task tool, chat). False when the task was not the user's
    or not active. Claims the status first, so a finishing task either wins or delivers nothing."""
    from mavis.channels.progress_card import hook_cards
    from mavis.domain.progress import CardFinal
    from mavis.store.repo import approvals, tasks

    if not await tasks.cancel(user_id, task_id):
        return False
    await approvals.reject_open_for_task(task_id)
    await cancel(task_id)
    await hook_cards().finalize(task_id, CardFinal.CANCELLED)
    return True
