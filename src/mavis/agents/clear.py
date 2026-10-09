"""/clear: start fresh in the chat (also offered from /settings).

Two choices behind an inline keyboard:
- "Clear this chat": cancel running tasks and open approvals, drop the stored conversation transcript and its
  summaries, and delete the messages Mavis may delete in the Telegram chat (the bot's and the user's, only
  within Telegram's 48 hour window, in deleteMessages batches of 100). Memory, knowledge and connections stay.
- "Start fresh (forget everything)": after a warning, the account deletion job (access.deletion) in reset
  mode: it clears the chat, erases every store, then re-admits the same chat as a new user (an owner chat
  stays an owner, an invited user stays admitted) and starts onboarding.

Telegram only. A Slack turn gets a short reply saying so (docs/LIMITATIONS.md).

Which messages can be deleted: the ids Mavis noted (channels.sent_log for its own sends, `_note_inbound` for
the user's) and, for anything it never saw, a sliding range of ids below the newest known one. Telegram
skips ids that are gone or older than 48 hours, so the range is cheap and safe; it stops after repeated
refusals. Deleting is best effort and never an error for the user.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from mavis.access.commands import register_user_command
from mavis.agents.buttons import register_button_handler
from mavis.channels import get_channel, routing
from mavis.channels.base import ChannelRateLimited
from mavis.config import get_settings
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Button, Outbound
from mavis.store.db import utcnow
from mavis.store.models import User
from mavis.store.repo import approvals, audit, chat_ids, messages, outbox, tasks, users
from mavis.worker.gates import register_event_gate

log = structlog.get_logger(__name__)

PREFIX = "clr:"
KEY = "clear"  # users.state["clear"] = {"asks": [[iso, event_id]], "menu_at": iso, "full_at": iso}
TELEGRAM_WINDOW = timedelta(hours=48)
WINDOW = TELEGRAM_WINDOW - timedelta(hours=1)  # an hour of margin for clock skew and slow batches
BATCH = 100  # deleteMessages: 1-100 ids per call
MAX_BATCH_FAILURES = 2  # consecutive refused or failed batches before the sweep stops
RATE_LIMIT_PAUSE_S = 10.0
RATE_WINDOW = timedelta(hours=1)
CONFIRM_TTL = timedelta(minutes=10)

MENU_TEXT = "What would you like to clear?"
FULL_WARNING = ("Start fresh forgets everything: your memories, notes, reminders, tasks and connected "
                "accounts are deleted and disconnected, and this chat is cleared. You will go through "
                "setup again. This can't be undone.")
CLEARED_TEXT = ("Done. I cleared our conversation history and deleted the recent messages in this chat "
                "that Telegram lets me delete (the last 48 hours). Your memories, knowledge and connected "
                "accounts are kept.\n\nTelegram doesn't let me remove older messages. To clear those too, "
                "tap the chat name (or the menu) at the top of this chat, then choose Clear history.")
GREETING = "Fresh start. What can I help with?"
CANCELLED_TEXT = "Okay, nothing was cleared."
TOO_OFTEN_TEXT = ("You've asked to clear several times in the last hour. Give it a little while, "
                  "then try again.")
STALE_TEXT = "That menu has expired. Send /clear to start again."
TELEGRAM_ONLY_TEXT = ("Clearing is only available in Telegram. In Slack I keep our messages, but you can "
                      "ask me to forget something specific.")


@dataclass
class SweepReport:
    requested: int = 0  # ids sent to Telegram (it skips the ones already gone)
    batches: int = 0
    refused: int = 0  # batches Telegram refused or that failed
    stopped_early: bool = False


# --- deleting messages ------------------------------------------------------------------------------------


def _candidates(known: list[int], newest: int | None, span: int) -> list[int]:
    """Ids to try, newest first: every id noted inside the window, then the sliding range of `span` ids below
    the newest known id that were never noted (a message sent before ids were stored, or that failed to
    record)."""
    out = list(dict.fromkeys(sorted(known, reverse=True)))
    if newest is not None and span > 0:
        seen = set(out)
        out += [m for m in range(newest, max(newest - span, 0), -1) if m not in seen]
    return out


async def delete_recent_messages(chat_id: int, *, extra_ids: tuple[int, ...] = (),
                                 now: datetime | None = None) -> SweepReport:
    """Delete what the bot can in a private chat: its own messages and the user's, sent less than 48 hours
    ago. Never raises for an undeletable message."""
    now = now or utcnow()
    channel = get_channel()
    known = await chat_ids.since(chat_id, now - WINDOW)
    newest = max([*known, *extra_ids, await chat_ids.latest(chat_id) or 0]) or None
    ids = _candidates([*known, *extra_ids], newest, get_settings().clear_fallback_span)
    report = SweepReport()
    failures = 0
    deleted_ok: list[int] = []
    for start in range(0, len(ids), BATCH):
        batch = ids[start:start + BATCH]
        ok = await _delete_batch(channel, chat_id, batch)
        report.batches += 1
        report.requested += len(batch)
        if ok:
            failures = 0
            deleted_ok += batch
            continue
        report.refused += 1
        failures += 1
        if failures >= MAX_BATCH_FAILURES:
            report.stopped_early = True
            break
    # forget what went (the ids sent), and anything noted that is already past the window
    await chat_ids.forget(chat_id, deleted_ok)
    await chat_ids.forget_before(chat_id, now - WINDOW)
    return report


async def _delete_batch(channel, chat_id: int, batch: list[int]) -> bool:
    for attempt in range(2):
        try:
            return bool(await channel.delete_messages(chat_id, batch))
        except ChannelRateLimited as exc:
            if attempt == 0:
                await asyncio.sleep(min(exc.retry_after, RATE_LIMIT_PAUSE_S))
                continue
            return False
        except Exception as exc:  # noqa: BLE001 - undeletable messages are never the user's problem
            log.warning("clear.delete_failed", error=type(exc).__name__)
            return False
    return False


# --- clearing the chat ------------------------------------------------------------------------------------


async def close_open_work(user_id: int) -> dict[str, int]:
    """Cancel running tasks (their progress cards close as cancelled) and reject open approvals, so nothing
    keeps working on, or asking about, a conversation that no longer exists."""
    cancelled = 0
    for task in await tasks.active_for_user(user_id):
        from mavis.agents import cancellation

        if await cancellation.cancel_by_user(user_id, task.id):
            cancelled += 1
    from mavis.domain.tasks import ApprovalStatus

    rejected = 0
    for approval in await approvals.open_for_user(user_id):
        if await approvals.claim(approval.id, {ApprovalStatus.PENDING, ApprovalStatus.AWAITING_EDIT},
                                 ApprovalStatus.REJECTED):
            rejected += 1
    return {"tasks": cancelled, "approvals": rejected}


async def _drop_unsent(user_id: int) -> int:
    from sqlalchemy import delete

    from mavis.store.db import Session
    from mavis.store.models import OutboxMessage

    async with Session() as s:
        res = await s.execute(delete(OutboxMessage).where(OutboxMessage.user_id == user_id,
                                                          OutboxMessage.status == "pending"))
        await s.commit()
        return res.rowcount or 0


async def wipe_chat(user_id: int, chat_id: int | None, *, extra_ids: tuple[int, ...] = ()) -> dict:
    """Option 1 without the reply: close open work, forget the transcript, delete the messages. Memory,
    knowledge and connections are not touched."""
    report: dict = {"closed": await close_open_work(user_id), "unsent": await _drop_unsent(user_id),
                    "history": await messages.clear_history(user_id)}
    await users.modify_nested(user_id, "settings", lambda cur: {**cur, "await": None})
    if chat_id is not None:
        sweep = await delete_recent_messages(chat_id, extra_ids=extra_ids)
        report["sweep"] = {"requested": sweep.requested, "batches": sweep.batches, "refused": sweep.refused}
    await audit.record(user_id, actor="user", action="chat.cleared", detail=report)
    return report


async def _say(user_id: int, text: str, key: str, buttons: list[list[Button]] | None = None) -> None:
    await outbox.enqueue_now(Outbound(user_id=user_id, text=text, buttons=buttons or [],
                                      dedupe_key=f"clear:{user_id}:{key}"))


def _event_message_ids(event: Event) -> tuple[int, ...]:
    mid = event.payload.get("message_id")
    return (int(mid),) if isinstance(mid, int) else ()


async def clear_this_chat(event: Event, user: User) -> None:
    await wipe_chat(user.id, user.telegram_chat_id, extra_ids=_event_message_ids(event))
    await _say(user.id, CLEARED_TEXT, f"done:{event.id}")
    await _say(user.id, GREETING, f"hello:{event.id}")


# --- asking, rate limit, buttons --------------------------------------------------------------------------


async def _allow(user_id: int, event_id: str) -> bool:
    """Count one ask against the per-user hourly limit; the same event again (a retry) counts once."""
    now = utcnow()
    verdict = {"ok": True}

    def change(cur: dict) -> dict:
        asks = [a for a in cur.get("asks", []) if now - datetime.fromisoformat(a[0]) < RATE_WINDOW]
        if any(a[1] == event_id for a in asks):
            return {**cur, "asks": asks}
        if len(asks) >= max(get_settings().clear_max_per_hour, 1):
            verdict["ok"] = False
            return {**cur, "asks": asks}
        return {**cur, "asks": [*asks, [now.isoformat(), event_id]], "menu_at": now.isoformat()}

    await users.modify_nested(user_id, KEY, change)
    return verdict["ok"]


async def _take(user_id: int, field: str) -> bool:
    """Use up a menu or warning that is still fresh. One tap works once; a stale or repeated tap does not."""
    now = utcnow()
    verdict = {"ok": False}

    def change(cur: dict) -> dict:
        at = cur.get(field)
        if at and now - datetime.fromisoformat(at) <= CONFIRM_TTL:
            verdict["ok"] = True
            return {**cur, field: None}
        return cur

    await users.modify_nested(user_id, KEY, change)
    return verdict["ok"]


def _menu() -> list[list[Button]]:
    return [[Button(label="Clear this chat", data="clr:chat")],
            [Button(label="Start fresh (forget everything)", data="clr:full")],
            [Button(label="Cancel", data="clr:no")]]


async def ask(event: Event, user: User) -> None:
    if event.source == routing.SLACK_SOURCE:
        await _say(user.id, TELEGRAM_ONLY_TEXT, f"slack:{event.id}")
        return
    if not await _allow(user.id, event.id):
        await _say(user.id, TOO_OFTEN_TEXT, f"limit:{event.id}")
        return
    await _say(user.id, MENU_TEXT, f"menu:{event.id}", _menu())


async def _clear_command(event: Event, user: User, args: list[str]) -> str | None:
    await ask(event, user)
    return None


async def on_button(event: Event, data: str) -> None:
    uid = event.user_id
    user = await users.get(uid)
    if event.source == routing.SLACK_SOURCE:
        await _say(uid, TELEGRAM_ONLY_TEXT, f"slack:{event.id}")
        return
    if data == "clr:ask":  # from /settings
        await ask(event, user)
    elif data == "clr:no":
        await users.modify_nested(uid, KEY, lambda cur: {**cur, "menu_at": None, "full_at": None})
        await _say(uid, CANCELLED_TEXT, f"no:{event.id}")
    elif data == "clr:chat":
        if await _take(uid, "menu_at"):
            await clear_this_chat(event, user)
        else:
            await _say(uid, STALE_TEXT, f"stale:{event.id}")
    elif data == "clr:full":
        if await _take(uid, "menu_at"):
            await users.modify_nested(uid, KEY, lambda cur: {**cur, "full_at": utcnow().isoformat()})
            await _say(uid, FULL_WARNING, f"warn:{event.id}",
                       [[Button(label="Yes, forget everything", data="clr:yes")],
                        [Button(label="Cancel", data="clr:no")]])
        else:
            await _say(uid, STALE_TEXT, f"stale:{event.id}")
    elif data == "clr:yes":
        if await _take(uid, "full_at"):
            from mavis.access import deletion

            await deletion.request_deletion(uid, reset=True)
        else:
            await _say(uid, STALE_TEXT, f"stale:{event.id}")


# --- remembering the user's message ids -------------------------------------------------------------------


async def _note_inbound(event: Event) -> bool:
    """Event gate (order 5): note the id of each Telegram message the user sends, so /clear can delete it.
    Never drops an event and never fails one."""
    if event.type is not EventType.USER_MESSAGE or event.source != "telegram" or event.user_id == 0:
        return True
    mid = event.payload.get("message_id")
    if not isinstance(mid, int):
        return True
    try:
        chat = (await users.get(event.user_id)).telegram_chat_id
        if chat is not None:
            await chat_ids.record(chat, [mid], "in", event.occurred_at)
    except Exception as exc:  # noqa: BLE001
        log.debug("clear.note_inbound_failed", error=type(exc).__name__)
    return True


async def _forget_chat_ids(user_id: int) -> dict:
    chat = (await users.get(user_id)).telegram_chat_id
    return {"ids": await chat_ids.forget(chat) if chat is not None else 0}


def register() -> None:
    from mavis.store.repo.deletion import register_deletion_step

    register_user_command("clear", _clear_command)
    register_button_handler(PREFIX, on_button)
    register_event_gate("chat_ids", _note_inbound, order=5)
    register_deletion_step("chat_ids", _forget_chat_ids)
