"""Telegram documents -> the user's workspace inbox/ (spec 6.1, slice B6). Runs as the first USER_MESSAGE
handler, so a task started by the same message already finds the file. Uploads are untrusted
(forwarded files are third-party content)."""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

import structlog

from mavis import machine
from mavis.channels import get_channel
from mavis.config import get_settings
from mavis.domain.events import Event
from mavis.domain.messages import Outbound
from mavis.machine.errors import QuotaExceeded
from mavis.machine.paths import safe_name
from mavis.machine.ports import Provenance
from mavis.store.db import utcnow
from mavis.store.repo import machine as repo
from mavis.store.repo import outbox

log = structlog.get_logger(__name__)
TOO_BIG_TEXT = (
    "That file is over {mb} MB, which is more than Telegram lets me download. "
    "Could you send a smaller one or a link?"
)
DOWNLOAD_FAILED_TEXT = "I couldn't download that file. Could you send it again?"


def user_allowed(user_id: int) -> bool:
    """MACHINE_USERS is an allowlist of user ids; empty means every user."""
    users = get_settings().machine_users
    return not users or user_id in users


def machine_allowed(user_id: int) -> bool:
    s = get_settings()
    return s.machine_enabled and machine.get_runtime() is not None and user_allowed(user_id)


async def on_user_message(event: Event) -> None:
    file = event.payload.get("file")
    if not file or not machine_allowed(event.user_id):
        return
    s = get_settings()
    limit = s.max_upload_mb * 1024 * 1024
    if int(file.get("size") or 0) > limit:
        await outbox.enqueue_now(
            Outbound(
                user_id=event.user_id,
                text=TOO_BIG_TEXT.format(mb=s.max_upload_mb),
                dedupe_key=f"intake:{event.id}:too_big",
            )
        )
        return
    name = safe_name(str(file.get("file_name") or "file"))
    rt = machine.get_runtime()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / name
            await get_channel().download_file(str(file["file_id"]), str(dest))
            data = dest.read_bytes()
    except Exception as exc:  # noqa: BLE001 - a failed download must not break the turn
        log.warning("machine.intake_failed", user_id=event.user_id, error=type(exc).__name__)
        await outbox.enqueue_now(
            Outbound(user_id=event.user_id, text=DOWNLOAD_FAILED_TEXT, dedupe_key=f"intake:{event.id}:failed")
        )
        return
    if len(data) > limit:
        return
    try:
        await rt.store.put(event.user_id, f"inbox/{name}", data, provenance=Provenance.USER_UPLOAD)
    except QuotaExceeded as exc:  # storage is full or the file is over the limit: say so plainly
        await outbox.enqueue_now(
            Outbound(user_id=event.user_id, text=exc.user_text, dedupe_key=f"intake:{event.id}:quota")
        )
        return
    log.info("machine.intake", user_id=event.user_id, size=len(data))


async def inbox_context(user_id: int, text: str) -> str:
    if not machine_allowed(user_id):
        return ""
    since = utcnow() - timedelta(hours=24)
    rows = [r for r in await repo.list_files(user_id, "inbox/") if r.updated_at and r.updated_at >= since]
    if not rows:
        return ""
    rows.sort(key=lambda r: r.updated_at, reverse=True)
    now = utcnow()
    items = [f"{Path(r.path).name} ({_kb(r.size)}, {_ago(now - r.updated_at)})" for r in rows[:5]]
    return "Files the user sent recently (in their Mavis AI workspace inbox): " + ", ".join(items)


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"


def _ago(d: timedelta) -> str:
    m = int(d.total_seconds() // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 60 else f"{m // 60} h ago"
