"""/delete_me (spec 10): confirm with buttons, mark deleting, run idempotent steps in a job; each step is
recorded in users.state["deletion"] so a crash resumes where it stopped."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from mavis import bus
from mavis.agents.buttons import register_button_handler
from mavis.domain import redis_keys
from mavis.domain.events import Event, Job, JobKind
from mavis.domain.messages import Button, Outbound
from mavis.store.db import utcnow
from mavis.store.repo import audit, outbox, users
from mavis.store.repo import deletion as repo

log = structlog.get_logger(__name__)
CONFIRM_TEXT = ("This deletes everything I know about you: messages, memories, reminders, connected "
                "accounts. It can't be undone.")
DONE_TEXT = "Done. Everything is deleted. If you ever want to come back, you'll need a new invite."
PRIVACY_TEXT = "Here's how Mavis AI handles your data: {url}"
CONFIRM_TTL = timedelta(minutes=10)


async def start(user_id: int, event_id: str) -> None:
    await users.update_nested(user_id, "deletion", {"asked_at": utcnow().isoformat()})
    await outbox.enqueue_now(Outbound(user_id=user_id, text=CONFIRM_TEXT, dedupe_key=f"del:ask:{event_id}",
                                      buttons=[[Button(label="Delete everything", data=f"del:yes:{user_id}"),
                                                Button(label="Cancel", data=f"del:no:{user_id}")]]))


async def confirm(user_id: int, data: str) -> bool:
    asked = (await users.get_state(user_id)).get("deletion", {}).get("asked_at")
    if not data.endswith(f":{user_id}") or asked is None:
        return False
    if utcnow() - datetime.fromisoformat(asked) > CONFIRM_TTL:
        return False
    if data.startswith("del:no:"):
        await users.update_nested(user_id, "deletion", {"asked_at": None})
        return False
    await request_deletion(user_id)
    return True


async def mark_deleting(user_id: int, *, by_owner: bool = False, reset: bool = False) -> None:
    await users.update(user_id, status="deleting")  # the gate now drops every event for this user
    # kept on the row, not only in the job, so a deletion resumed after a restart is still a reset
    await users.modify_nested(user_id, "deletion", lambda cur: {**cur, "reset": reset})
    from mavis.web import sessions

    await sessions.delete_all(user_id)  # and no browser stays signed in
    await audit.record(user_id, actor="owner" if by_owner else "user",
                       action="user.reset_requested" if reset else "user.delete_requested", detail={})


async def request_deletion(user_id: int, *, by_owner: bool = False, reset: bool = False) -> None:
    """Start the erase job. `reset` makes it a fresh start: the same chat is re-admitted afterwards."""
    await mark_deleting(user_id, by_owner=by_owner, reset=reset)
    await bus.get_bus().enqueue(Job(id=f"delete:{user_id}", user_id=user_id, kind=JobKind.DELETE_USER,
                                    payload={"reset": reset} if reset else {}))


async def _redis(user_id: int) -> dict:
    from sqlalchemy import delete, update

    from mavis.store.db import Session
    from mavis.store.models import OutboxMessage, WakeupRow

    async with Session() as s:
        await s.execute(update(WakeupRow).where(WakeupRow.user_id == user_id, WakeupRow.status == "pending")
                        .values(status="cancelled"))
        await s.execute(delete(OutboxMessage).where(OutboxMessage.user_id == user_id,
                                                    OutboxMessage.status.in_(("pending", "sending"))))
        await s.commit()
    client = bus.get_redis()
    n = 0
    if client is not None:
        async for key in client.scan_iter(match=redis_keys.user_pattern(user_id), count=500):
            n += int(await client.delete(key))
        from mavis.agents.reactions import ReactionLog

        n += int(await client.delete(ReactionLog._key(user_id)))  # the one per-user key outside the pattern
    return {"keys": n}


async def _markers(user_id: int) -> dict:
    """Learn markers (processed_events): records are keyed by user (learn:<uid>:<source_ref>), chat turns
    by the message's event id. They hold no text, but they name the user's messages, so they go too."""
    from sqlalchemy import select

    from mavis.store.db import Session
    from mavis.store.models import Message
    from mavis.store.repo import events

    async with Session() as s:
        refs = list(await s.scalars(select(Message.event_id).where(
            Message.user_id == user_id, Message.event_id.is_not(None))))
    n = await events.forget_prefix(f"learn:{user_id}:")
    for ref in refs:
        n += await events.forget_prefix(f"learn:{ref}")
    return {"markers": n}


async def _native(user_id: int) -> dict:
    """Native Google and Slack grants: tell the vendor to drop each one (best effort), then erase the rows
    (user grants, the workspace bot grant, in-flight consent states). The sweep at the end also covers a
    deployment that no longer runs the native router."""
    from sqlalchemy import delete, func, select

    from mavis.store.db import Session
    from mavis.store.models import NativeGrant, NativeOAuthState
    from mavis.tools.integrations import get_provider
    from mavis.tools.integrations.native.base import NativeProvider

    oauth = getattr(get_provider(), "oauth", None)
    revoked = 0
    if oauth is not None:
        for provider in (NativeProvider.GOOGLE, NativeProvider.SLACK):
            try:
                if await oauth.tokens.grant(user_id, provider) is not None:
                    await oauth.revoke(user_id, provider)
                    revoked += 1
            except Exception as exc:  # noqa: BLE001 - the local erase below still removes the grant
                log.warning("deletion.native_revoke_failed", provider=provider.value,
                            error=type(exc).__name__)
    async with Session() as s:
        left = int(await s.scalar(select(func.count()).select_from(NativeGrant).where(
            NativeGrant.user_id == user_id)) or 0)
        await s.execute(delete(NativeGrant).where(NativeGrant.user_id == user_id))
        gone = await s.execute(delete(NativeOAuthState).where(NativeOAuthState.user_id == user_id))
        states = gone.rowcount
        await s.commit()
    return {"revoked": revoked, "swept": left, "states": states or 0}


async def _composio(user_id: int) -> dict:
    from mavis.domain.integrations import UserRef
    from mavis.tools.integrations import get_provider

    removed = 0
    for attempt in range(5):
        try:
            removed = await get_provider().disconnect_all(UserRef(user_id=user_id))
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("deletion.composio_retry", attempt=attempt, error=type(exc).__name__)
    else:
        from mavis.obs.watchdog import alert_owner

        await alert_owner(f"Composio disconnect failed for deleted user #{user_id}",
                          key=f"del:comp:{user_id}")
    return {"accounts": removed}


async def _vector(user_id: int) -> dict:
    from mavis.memory.service import get_memory

    return {"points": await get_memory().vector.delete_user(user_id)}


async def _graph(user_id: int) -> dict:
    from mavis.memory.service import get_memory

    return {"nodes": await get_memory().graph.delete_user(user_id)}


async def _artifacts(user_id: int) -> dict:
    import shutil

    from mavis.store.artifacts import user_dir

    d = user_dir(user_id)
    existed = d.exists()
    shutil.rmtree(d, ignore_errors=True)
    return {"artifacts_dir": int(existed)}


async def _checkpoints(user_id: int) -> dict:
    """LangGraph keeps a task's whole state (goal text, step results) under thread_id task:<id>."""
    from sqlalchemy import select

    from mavis.agents.checkpointing import open_checkpointer
    from mavis.store.db import Session
    from mavis.store.models import Task

    async with Session() as s:
        ids = list(await s.scalars(select(Task.id).where(Task.user_id == user_id)))
    if not ids:
        return {"threads": 0}
    async with open_checkpointer() as saver:
        for task_id in ids:
            await saver.adelete_thread(f"task:{task_id}")
    return {"threads": len(ids)}


def _steps():
    yield "redis", _redis
    yield "composio", _composio
    yield "native", _native
    yield from repo.EXTERNAL_STEPS.items()
    yield "markers", _markers
    yield "checkpoints", _checkpoints
    yield "vector", _vector
    yield "graph", _graph
    yield "artifacts", _artifacts


async def run_steps(user_id: int, *, reset: bool = False) -> dict:
    """Erase the user from every store. With `reset` the chat is cleared first and, at the end, the same chat
    is re-admitted as a new user (see `_readmit`) instead of leaving a deleted tombstone."""
    before = await users.get(user_id)
    report: dict = {}
    if reset and "chat" not in (before.state or {}).get("deletion", {}).get("done", []):
        from mavis.agents.clear import wipe_chat

        report["chat"] = await wipe_chat(user_id, before.telegram_chat_id)
        await users.modify_nested(user_id, "deletion",
                                  lambda cur: {**cur, "done": [*cur.get("done", []), "chat"]})
    for name, fn in _steps():
        done = (await users.get_state(user_id)).get("deletion", {}).get("done", [])
        if name in done:
            continue
        report[name] = await fn(user_id)
        await users.modify_nested(user_id, "deletion",
                                  lambda cur, n=name: {**cur, "done": [*cur.get("done", []), n]})
    chat = (await users.get(user_id)).telegram_chat_id
    report["postgres"] = await repo.delete_user_rows(user_id)
    if reset:
        await _readmit(before)
    else:
        if chat is not None:
            from mavis.channels import get_channel

            try:
                await get_channel().send_text(chat, DONE_TEXT)
            except Exception as exc:  # noqa: BLE001
                log.warning("deletion.final_message_failed", error=type(exc).__name__)
        await users.update(user_id, status="deleted", name=None, state={}, telegram_chat_id=None,
                           telegram_user_id=None, composio_user_id=None, deleted_at=utcnow(), locale=None,
                           currency=None, country=None)
    await audit.record(user_id, actor="system", action="user.reset" if reset else "user.deleted",
                       detail={k: (sum(v.values()) if isinstance(v, dict) else v)
                               for k, v in report.items() if k != "chat"})
    if reset:
        user = await users.get(user_id)
        from mavis.agents import onboarding

        await onboarding.start(user, None)
    return report


async def _readmit(before) -> None:
    """After a reset the same chat is a new user right away. The owner chat comes back as an active owner
    (the OWNER_TELEGRAM_CHAT_IDS grandfathering); anyone else keeps the tier they were admitted with and is
    active without a new invite. Everything personal stays erased."""
    from mavis.access import UserStatus, UserTier
    from mavis.config import get_settings
    from mavis.tools.integrations.identity import new_provider_id

    s = get_settings()
    owner = before.telegram_chat_id is not None and before.telegram_chat_id in s.owner_telegram_chat_ids
    await users.update(before.id, status=UserStatus.ACTIVE.value,
                       tier=UserTier.OWNER.value if owner else before.tier, name=None, state={},
                       telegram_chat_id=before.telegram_chat_id, telegram_user_id=before.telegram_user_id,
                       composio_user_id=new_provider_id(before.id), deleted_at=None, banned_at=None,
                       ban_reason=None, activated_at=utcnow(), inactive_since=None, locale=None,
                       currency=None, country=None, timezone=s.default_timezone, onboarded=False)


async def resume_deletions() -> int:
    """Worker start: an account still marked deleting lost its job (a deploy or crash mid-erase, or the job
    ran out of attempts), which left it half erased and locked out (evals 2026-10-10: a reset stopped after
    five of ten steps). Each is enqueued again; the steps record what is done, so they pick up where they
    stopped."""
    from sqlalchemy import select

    from mavis.store.db import Session
    from mavis.store.models import AuditLog, User

    async with Session() as s:
        ids = list(await s.scalars(select(User.id).where(User.status == "deleting")))
    for user_id in ids:
        state = (await users.get_state(user_id)).get("deletion", {})
        if "reset" in state:
            reset = bool(state["reset"])
        else:  # marked before the flag was kept on the row: what they asked for is in the audit log
            async with Session() as s:
                asked = await s.scalar(select(AuditLog.action).where(
                    AuditLog.user_id == user_id,
                    AuditLog.action.in_(("user.reset_requested", "user.delete_requested")))
                    .order_by(AuditLog.id.desc()).limit(1))
            reset = asked == "user.reset_requested"
        await bus.get_bus().enqueue(Job(id=f"delete:{user_id}:resume:{int(utcnow().timestamp())}",
                                        user_id=user_id, kind=JobKind.DELETE_USER,
                                        payload={"reset": reset} if reset else {}))
    if ids:
        log.info("deletion.resumed", count=len(ids))
    return len(ids)


async def run_deletion(job: Job) -> None:
    if (await users.get(job.user_id)).status != "deleting":
        return  # a retried job after it already finished: the account is deleted, or reset and in use again
    await run_steps(job.user_id, reset=bool(job.payload.get("reset")))


async def _delete_cmd(event: Event, user, args) -> str | None:
    await start(user.id, event.id)
    return None


async def _privacy_cmd(event: Event, user, args) -> str:
    from mavis.config import get_settings

    return PRIVACY_TEXT.format(url=get_settings().privacy_url)


async def _button(event: Event, data: str) -> None:
    await confirm(event.user_id, data)


def register() -> None:
    from mavis.access.commands import register_owner_command, register_user_command
    from mavis.worker.runner import register_job_handler, register_startup_hook

    register_user_command("delete_me", _delete_cmd)
    register_user_command("deleteme", _delete_cmd)
    register_user_command("privacy", _privacy_cmd)
    register_button_handler("del:", _button)
    register_job_handler(JobKind.DELETE_USER, run_deletion)
    register_startup_hook(resume_deletions)

    async def admin_delete(event, owner, args):
        if len(args) >= 2 and args[0] == "delete" and args[1].isdigit():
            await request_deletion(int(args[1]), by_owner=True)
            return f"Deleting user #{args[1]}."
        return None

    register_owner_command("admin_delete", admin_delete)
