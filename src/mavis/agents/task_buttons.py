"""`tk:<task_id>:x` taps on a progress card. Only the task's owner may cancel; the tap is acknowledged at
ingest (spinner stops), so no second message is sent."""

from __future__ import annotations

import re

import structlog

from mavis.agents import cancellation
from mavis.agents.buttons import register_button_handler
from mavis.domain.events import Event
from mavis.domain.progress import TASK_BUTTON_PREFIX
from mavis.store.repo import tasks

log = structlog.get_logger(__name__)
_DATA = re.compile(r"tk:(\d{1,18}):x")


async def handle_task_button(event: Event, data: str) -> None:
    m = _DATA.fullmatch(data)
    if not m:
        log.info("task_button.malformed")
        return
    task_id = int(m.group(1))
    task = await tasks.get(task_id)
    if task is None or task.user_id != event.user_id:
        log.warning("task_button.not_owner", task_id=task_id)
        return
    await cancellation.cancel_by_user(event.user_id, task_id)


def register_task_buttons() -> None:
    register_button_handler(TASK_BUTTON_PREFIX, handle_task_button)
