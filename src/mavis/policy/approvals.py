"""Approval UX: prompts with buttons, button/text decisions, reminders and expiry.

Decisions never execute anything here. They move the approval to RESOLVING
(atomically, so double taps are harmless) and enqueue RESUME_TASK; the
orchestrator's approval_gate performs the action.
"""

from __future__ import annotations

from datetime import timedelta

import structlog

from mavis.config import get_settings
from mavis.domain.messages import Button, Outbound, Role
from mavis.domain.tasks import ApprovalStatus
from mavis.store.db import Session, utcnow
from mavis.store.repo import approvals, messages, outbox
from mavis.timers import service as timers_service

log = structlog.get_logger()


def approval_buttons(approval_id: int) -> list[list[Button]]:
    return [[
        Button(label="✅ Send", data=f"ap:{approval_id}:ok"),
        Button(label="✏️ Edit", data=f"ap:{approval_id}:edit"),
        Button(label="❌ Cancel", data=f"ap:{approval_id}:no"),
    ]]


async def say(user_id: int, text: str, buttons: list[list[Button]] | None = None,
              dedupe_key: str | None = None) -> None:
    async with Session() as s:
        await outbox.enqueue(
            s, Outbound(user_id=user_id, text=text, buttons=buttons or [], dedupe_key=dedupe_key)
        )
        await s.commit()
    await messages.log(user_id, Role.ASSISTANT, text)


async def send_approval_prompt(user_id: int, payload: dict) -> None:
    approval = await approvals.get(int(payload["approval_id"]))
    if approval is None or approval.status != ApprovalStatus.PENDING:
        return
    text = f"Ready when you are. Want me to go ahead?\n\n{approval.preview}"
    await say(user_id, text, approval_buttons(approval.id),
              dedupe_key=f"approval:{approval.id}:{int(utcnow().timestamp() * 1000)}")
    if await approvals.mark_prompted(approval.id):
        ttl = timedelta(hours=get_settings().approval_ttl_hours)
        now = utcnow()
        wakeups = timers_service.WakeupService()
        await wakeups.wake_me(user_id, now + ttl - timedelta(hours=2), f"approval:{approval.id}",
                              kind="system_approval_remind", scale=False,
                              dedupe_key=f"approval:{approval.id}:remind")
        await wakeups.wake_me(user_id, now + ttl, f"approval:{approval.id}", kind="system_approval_expire",
                              scale=False, dedupe_key=f"approval:{approval.id}:expire")
