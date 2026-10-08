"""Deliver finished task results to the user (TASK_COMPLETED -> outbox).

User-requested results always go out. Initiative-originated results are unsolicited, so they pass the
ping policy and are deferred via a wakeup when the policy says not now. A task that read third-party
content (tainted) is delivered as untrusted: links, addresses, payment ids, phone numbers and codes are
scrubbed from its text deterministically.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from mavis.config import get_settings
from mavis.domain.events import Event
from mavis.domain.messages import TAINT_SUFFIX, Outbound, Role
from mavis.domain.tasks import REPORTED_STATUSES, TaskOrigin
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


def artifact_key(task_id: int, artifact_id: int) -> str:
    """Outbox key of one file: the same whether it went out mid-task or with the result (sent once)."""
    return f"task:{task_id}:art:{artifact_id}"


def too_big_line(name: str, size: int) -> str:
    mb = max(1, round(size / (1024 * 1024)))
    return f"{name} is {mb} MB, too big to send here. It's saved in your files."


async def _enqueue_artifact(s: AsyncSession, user_id: int, task_id: int, art: Any, proactive: bool) -> None:
    limit = get_settings().machine_file_max_mb * 1024 * 1024
    name = Path(art.path).name
    if (art.size or 0) > limit:
        msg = Outbound(user_id=user_id, text=too_big_line(name, art.size), proactive=proactive,
                       dedupe_key=artifact_key(task_id, art.id))
    else:
        msg = Outbound(user_id=user_id, text=name, document_path=art.path, proactive=proactive,
                       dedupe_key=artifact_key(task_id, art.id))
    await outbox.enqueue(s, msg)


async def deliver_artifact_now(user_id: int, task_id: int, artifact_id: int, *, proactive: bool = False) -> bool:
    """Send one file of a task right away. True when this call sent it; never twice (key + delivered_at)."""
    art = next((a for a in await tasks.artifacts_for(task_id) if a.id == artifact_id), None)
    if art is None or art.user_id != user_id or art.delivered_at is not None:
        return False
    async with Session() as s:
        await _enqueue_artifact(s, user_id, task_id, art, proactive)
        await s.commit()
    if not await tasks.mark_delivered(artifact_id):
        return False
    from mavis.channels.progress_card import hook_cards  # lazy: channels import the bus

    await hook_cards().file_sent(task_id)
    return True


async def deliver_pending_artifacts(user_id: int, task_id: int, *, proactive: bool = False) -> int:
    """Every file of the task not sent yet, in the order they were made. Returns how many went out."""
    sent = 0
    for art in await tasks.undelivered_artifacts(task_id):
        sent += int(await deliver_artifact_now(user_id, task_id, art.id, proactive=proactive))
    return sent


async def _record_payload_files(user_id: int, task_id: int, paths: list[str]) -> None:
    """A path in the event payload with no artifact row yet (older callers) gets one, so delivery runs
    from the database only and a file can never go out twice under two keys."""
    recorded = {a.path for a in await tasks.artifacts_for(task_id)}
    for path in dict.fromkeys(str(p) for p in paths):
        if path in recorded:
            continue
        await tasks.add_artifact(task_id, user_id, kind=Path(path).suffix.lstrip(".") or "file", path=path,
                                 mime=mimetypes.guess_type(path)[0] or "application/octet-stream")


async def _send(user_id: int, task_id: int, texts: list[str], proactive: bool, tainted: bool = False) -> None:
    async with Session() as s:
        for i, text in enumerate(texts):
            await outbox.enqueue(s, Outbound(user_id=user_id, text=text, proactive=proactive,
                                             dedupe_key=f"task:{task_id}:m{i}"))
        await s.commit()
    await deliver_pending_artifacts(user_id, task_id, proactive=proactive)
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
    # Files are delivered from the artifact rows (tools and specialists record their own); files already
    # sent mid-task are skipped.
    await _record_payload_files(event.user_id, task_id, list(p.get("artifacts", [])))
    await _send(event.user_id, task_id, texts, proactive, tainted)
    if proactive:
        await pings.PingPolicy().record(user, f"task:{task_id}", DELIVERY_URGENCY, utcnow())


async def redeliver(user_id: int, task_id: int) -> None:
    """Called by the task_delivery wakeup once the ping policy window opens."""
    task = await tasks.get(task_id)
    # A task that finished through its responder, whatever its outcome (DONE, PARTIAL or FAILED), has a
    # report to deliver; a crashed or stalled one has none (the user was told when it failed).
    if (task is None or task.user_id != user_id or task.status not in REPORTED_STATUSES
            or task.result_text is None):
        return
    tainted = bool(task.tainted)
    texts = _clean((task.result_text or "").split("\n\n"), tainted)
    await _send(user_id, task_id, texts, proactive=True, tainted=tainted)
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
