"""Phase 4 handler registration: chat turns, approval buttons, task jobs, progress and system wakeups.

`register()` is idempotent and runs on every call (tests clear the registries between runs). It does not
register TASK_COMPLETED: that event has one owner, `tools.integrations.wiring.dispatch_task_completed`,
which hands task results to `task_delivery`. BUTTON_PRESSED has one handler, `buttons.dispatch_button`;
this module only adds the `ap:` prefix to it.
"""

from __future__ import annotations

import structlog

from mavis.agents import buttons, conversation, orchestrator
from mavis.domain.events import EventType, Job, JobKind
from mavis.initiative import task_delivery
from mavis.policy import approvals as approval_flow
from mavis.timers.system import register_system_wakeup
from mavis.worker.runner import register_event_handler, register_job_handler

log = structlog.get_logger(__name__)

TASK_DELIVERY_KIND = "system_task_delivery"


async def _run_task_job(job: Job) -> None:
    await orchestrator.run_task(int(job.payload["task_id"]))


async def _resume_task_job(job: Job) -> None:
    """Two payload shapes: the generic `{"task_id", "value"}` (ConnectFlow sends task_id as a str) and
    the approval decision `{"task_id", "approval_id", "decision", "instructions"}`."""
    p = job.payload
    value = p.get("value")
    if value is None:
        value = {"approval_id": int(p["approval_id"]), "decision": p["decision"],
                 "instructions": p.get("instructions", "")}
    await orchestrator.resume_task(int(p["task_id"]), value)


async def _task_delivery(user_id: int, reason: str) -> None:
    """A deferred proactive task result: the ping policy window is open again (reason `task:<id>`)."""
    prefix, _, raw_id = reason.partition(":")
    if prefix != "task" or not raw_id.isdigit():
        log.warning("task_delivery.bad_reason", reason=reason[:40])
        return
    await task_delivery.redeliver(user_id, int(raw_id))


def register() -> None:
    register_event_handler(EventType.USER_MESSAGE, conversation.run_turn, replace=True)
    register_event_handler(EventType.BUTTON_PRESSED, buttons.dispatch_button)
    buttons.register_approval_buttons()
    # Fixed text, no LLM call; replace=True takes TASK_PROGRESS away from the initiative reasoner.
    register_event_handler(EventType.TASK_PROGRESS, task_delivery.on_progress, replace=True)
    register_job_handler(JobKind.RUN_TASK, _run_task_job)
    register_job_handler(JobKind.RESUME_TASK, _resume_task_job)
    # Startup and morning sweeps plus the system_approval_remind / system_approval_expire wakeups.
    approval_flow.register_sweeps()
    register_system_wakeup(TASK_DELIVERY_KIND, _task_delivery)
