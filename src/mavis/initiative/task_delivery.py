"""Deliver finished task results to the user (TASK_COMPLETED -> outbox).

User-requested results always go out. Initiative-originated results are unsolicited, so they pass the
ping policy and are deferred via a wakeup when the policy says not now. A task that read third-party
content (tainted) is delivered as untrusted: links, addresses, payment ids, phone numbers and codes are
scrubbed from its text deterministically.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from mavis.domain.events import Event
from mavis.domain.messages import TAINT_SUFFIX, Outbound, Role
from mavis.domain.tasks import TaskOrigin, TaskStatus
from mavis.initiative.composer import scrub_untrusted_origin
from mavis.policy import pings
from mavis.store.db import Session, utcnow
from mavis.store.repo import messages, outbox, tasks, users
from mavis.timers import service as timers_service

log = structlog.get_logger()

PROGRESS_TEXT = "Still on it, this one's taking a bit. I'll send it over as soon as it's ready."
DELIVERY_URGENCY = 3


def history_event_id(task_id: int, index: int, tainted: bool) -> str:
    """History key for a delivered result line. A tainted task's lines carry the taint marker, so the
    next chat turn runs tainted and learns from them as untrusted. The key also dedupes a redelivery."""
    return f"task:{task_id}:m{index}{TAINT_SUFFIX if tainted else ''}"


async def _send(user_id: int, task_id: int, texts: list[str], artifacts: list[str], proactive: bool,
                tainted: bool = False) -> None:
    async with Session() as s:
        for i, text in enumerate(texts):
            await outbox.enqueue(s, Outbound(user_id=user_id, text=text, proactive=proactive,
                                             dedupe_key=f"task:{task_id}:m{i}"))
        for j, path in enumerate(artifacts):
            await outbox.enqueue(s, Outbound(user_id=user_id, text=Path(path).name, document_path=path,
                                             proactive=proactive, dedupe_key=f"task:{task_id}:a{j}"))
        await s.commit()
    for i, text in enumerate(texts):
        await messages.log(user_id, Role.ASSISTANT, text, proactive=proactive,
                           event_id=history_event_id(task_id, i, tainted))


def _clean(texts: list, tainted: bool) -> list[str]:
    out = [str(t).strip() for t in texts if str(t).strip()]
    return [scrub_untrusted_origin(t) for t in out] if tainted else out


async def deliver_task_result(event: Event) -> None:
    p = event.payload
    if not p.get("notify_on_complete", True):
        return
    task_id = int(p["task_id"])
    tainted = bool(p.get("tainted"))
    proactive = p.get("origin") == TaskOrigin.INITIATIVE
    user = None
    if proactive:
        user = await users.get(event.user_id)
        verdict = await pings.PingPolicy().check(user, urgency=DELIVERY_URGENCY, dedupe_key=f"task:{task_id}",
                                                 now=utcnow())
        if not verdict.allow:
            if verdict.defer_until is not None:
                await timers_service.WakeupService().wake_me(
                    event.user_id, verdict.defer_until, f"task:{task_id}", kind="system_task_delivery",
                    scale=False, dedupe_key=f"task_delivery:{task_id}",
                )
            return
    texts = _clean(p.get("messages", []), tainted)
    # DB rows include artefacts recorded directly by tools/specialists, not only those in the payload.
    recorded = [a.path for a in await tasks.artifacts_for(task_id)]
    artifacts = list(dict.fromkeys([*p.get("artifacts", []), *recorded]))
    await _send(event.user_id, task_id, texts, artifacts, proactive, tainted)
    if proactive:
        await pings.PingPolicy().record(user, f"task:{task_id}", DELIVERY_URGENCY, utcnow())


async def redeliver(user_id: int, task_id: int) -> None:
    """Called by the task_delivery wakeup once the ping policy window opens."""
    task = await tasks.get(task_id)
    if task is None or task.user_id != user_id or task.status != TaskStatus.DONE:
        return
    tainted = bool(task.tainted)
    texts = _clean((task.result_text or "").split("\n\n"), tainted)
    artifacts = [a.path for a in await tasks.artifacts_for(task_id)]
    await _send(user_id, task_id, texts, artifacts, proactive=True, tainted=tainted)
    await pings.PingPolicy().record(await users.get(user_id), f"task:{task_id}", DELIVERY_URGENCY, utcnow())


async def on_progress(event: Event) -> None:
    """A long user-requested task: one fixed line, no LLM call (it would queue behind the running task)."""
    p = event.payload
    if p.get("origin") != TaskOrigin.USER:
        return
    task_id = int(p["task_id"])
    async with Session() as s:
        await outbox.enqueue(s, Outbound(user_id=event.user_id, text=PROGRESS_TEXT,
                                         dedupe_key=f"task:{task_id}:progress"))
        await s.commit()
    await messages.log(event.user_id, Role.ASSISTANT, PROGRESS_TEXT, event_id=f"task:{task_id}:progress")
