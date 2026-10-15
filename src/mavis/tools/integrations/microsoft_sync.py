# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""First sync for a new Microsoft 365 account: Outlook mail, Outlook calendar, Teams chats and OneDrive.

Same rules as Gmail, Google Calendar, Slack and Notion: everything goes through the existing ingest path
(`ConnectorIngest`: structural filters, redaction, caps, one LEARN job per kept record), third-party content
never creates loops, and the learned records carry the forgettable origin prefixes `outlook:`, `ocal:`,
`teams:` and `onedrive:`. Every read is made on the Microsoft account only (`execute_native`), so an Outlook
sync can never be answered from Gmail. Each handler returns at most a few "what I noticed" lines.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.first_sync import (
    GMAIL_LEARN_JOBS,
    FirstSync,
    register_first_sync_handler,
    sync_window,
)
from mavis.tools.integrations.native import guard
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.normalize import extract_list, extract_messages, normalize_email, to_datetime

log = structlog.get_logger()

INBOX_MAX, SENT_MAX = 50, 25
OTHER_LABEL = "CATEGORY_OTHER"
PER_CHAT_CAP = 100
TOTAL_CAP = 600
BACKFILL_BUDGET_S = 10 * 60
MAX_CHATS = 30
HISTORY_PAGE = 50
DRIVE_MAX = 25
TITLE_CAP, TITLES_PER_LIST = 120, 50


async def native_call(provider: Any, user_id: int, action: str, args: dict) -> ToolResult:
    """One action on the Microsoft account only: the router's `execute_native` when the provider has it, else
    its plain `execute` (a test fake)."""
    user = UserRef(user_id=user_id)
    run = getattr(provider, "execute_native", None)
    if run is not None:
        return await run(NativeProvider.MICROSOFT, user, action, args)
    return await provider.execute(user, action, args)


class MicrosoftSync:
    def __init__(self, first_sync: FirstSync) -> None:
        self.fs = first_sync

    def register(self) -> None:
        register_first_sync_handler(Capability.OUTLOOK, self.outlook)
        register_first_sync_handler(Capability.OUTLOOK_CALENDAR, self.calendar)
        register_first_sync_handler(Capability.TEAMS, self.teams)
        register_first_sync_handler(Capability.ONEDRIVE, self.onedrive)

    async def _data(self, user_id: int, action: str, args: dict) -> Any:
        res = await native_call(self.fs.provider, user_id, action, args)
        return res.data if res.ok else None

    # --- Outlook mail ----------------------------------------------------------------------------------

    async def outlook(self, user_id: int) -> list[str]:
        days = sync_window("sync_outlook_days", 14)
        inbox = await self._data(user_id, "mail.search", {"query": f"in:inbox newer_than:{days}d",
                                                         "max_results": INBOX_MAX})
        sent = await self._data(user_id, "mail.search", {"query": f"in:sent newer_than:{days}d",
                                                        "max_results": SENT_MAX})
        if inbox is None and sent is None:
            return []
        mails: list[dict] = []
        for data in (inbox, sent):
            for m in extract_messages(data) if data is not None else []:
                n = normalize_email(m)
                if self._skip(n):
                    continue
                mails.append(n)
        fs = self.fs
        if fs.connectors is not None:
            for n in mails:  # the guard still decides per message (mute list, bulk headers, own mail)
                await fs.connectors.email(user_id, n, source="outlook")
        lines = [
            f"Email from {m['from']} ({(m['received_at'] or '')[:10]}): {m['subject']}: {m['snippet'][:160]}"
            for m in mails
            if "SENT" not in m["labels"]
        ]
        if fs.connectors is None:
            await fs._learn_batches(
                user_id, "Recent emails (untrusted content; extract people, organisations and events only):",
                lines, f"first_sync:{user_id}:outlook", GMAIL_LEARN_JOBS)
        return fs._mail_notices(mails, lines)

    @staticmethod
    def _skip(n: dict) -> bool:
        """The Other inbox tab (Outlook's promotions-like bucket) and bulk mail are not skimmed."""
        labels = {str(x).upper() for x in n.get("labels") or []}
        if "SENT" in labels:
            return False
        return OTHER_LABEL in labels or guard.should_ingest_email(n).lane == "bulk"

    # --- Outlook calendar ------------------------------------------------------------------------------

    async def calendar(self, user_id: int) -> list[str]:
        async def execute(uid: int, action: str, args: dict) -> Any:
            return await self._data(uid, action, args)

        return await self.fs._calendar(user_id, execute=execute, ref=f"ocal:{user_id}:first_sync")

    # --- Teams -----------------------------------------------------------------------------------------

    async def teams(self, user_id: int) -> list[str]:
        res = await native_call(self.fs.provider, user_id, "teams.chats", {"limit": MAX_CHATS})
        if not res.ok:  # no Teams licence, a personal account or a transient error: nothing to say
            return []
        sink, clock = self.fs.connectors, self.fs.clock
        if sink is None:
            log.warning("first_sync.teams_no_connectors", user_id=user_id)
        own_tid = await self._own_tenant(user_id)
        started = clock()
        cutoff = started - timedelta(days=sync_window("sync_teams_days", 7))
        chats = [c for c in extract_list(res.data, "chats", "data.chats") if isinstance(c, dict)]
        chats.sort(key=lambda c: str(c.get("lastMessageAt") or ""), reverse=True)
        kept = read_chats = 0
        for chat in chats[:MAX_CHATS]:
            if sink is None or kept >= TOTAL_CAP or (clock() - started).total_seconds() > BACKFILL_BUDGET_S:
                break
            last = to_datetime(chat.get("lastMessageAt"))
            if not chat.get("id") or (last is not None and last < cutoff):
                continue
            got = await self._chat(user_id, chat, cutoff, TOTAL_CAP - kept, own_tid, started)
            kept += got
            read_chats += 1 if got else 0
        await self._catch_up(user_id)
        return [f"Caught up on {kept} recent Teams messages in {read_chats} chats"] if kept else []

    async def _own_tenant(self, user_id: int) -> str:
        """The signed-in account's tenant id ("" when unknown: then nobody counts as internal)."""
        tokens = getattr(self.fs.provider, "tokens", None)
        try:
            acct = await tokens.account(user_id, NativeProvider.MICROSOFT) if tokens is not None else None
        except Exception:  # noqa: BLE001 - an unknown tenant only makes records lower trust
            return ""
        return str((acct or {}).get("tid") or "").lower()

    async def _chat(self, user_id: int, chat: dict, cutoff: Any, room: int, own_tid: str = "",
                    started: Any = None) -> int:
        """Page one chat newest first until the window, the per-chat cap or the total cap. A failed page
        (rate limited, unavailable, anything) ends this chat only: a partial chat is fine."""
        cid, members = str(chat["id"]), [m for m in chat.get("members") or [] if isinstance(m, dict)]
        roster = [{"name": str(m.get("name") or ""), "email": str(m.get("email") or ""),
                   "me": bool(m.get("me"))} for m in members]
        by_id = {str(m.get("id") or ""): m for m in members}
        clock = self.fs.clock
        started = started or clock()
        kept, before = 0, ""
        while kept < min(PER_CHAT_CAP, room) and (clock() - started).total_seconds() <= BACKFILL_BUDGET_S:
            args: dict[str, Any] = {"chat_id": cid, "limit": HISTORY_PAGE}
            if before:
                args["before"] = before
            res = await native_call(self.fs.provider, user_id, "teams.history", args)
            if not res.ok or not isinstance(res.data, dict):
                if res.error_kind in (FailureKind.RATE_LIMITED, FailureKind.UNAVAILABLE):
                    log.info("teams.sync_page_skipped", user_id=user_id, kind=res.error_kind.value)
                break
            done = False
            for m in res.data.get("messages") or []:
                at = to_datetime(m.get("at")) if isinstance(m, dict) else None
                if at is None:
                    continue
                if at < cutoff:  # newest first: everything after this is older still
                    done = True
                    break
                if kept >= min(PER_CHAT_CAP, room):
                    break
                from_id, mid = str(m.get("fromId") or ""), str(m.get("id") or "")
                if not mid:
                    continue
                sender = by_id.get(from_id, {})
                # a sender outside the account's tenant, or whose tenant is unknown, is external (low trust)
                tid = str(sender.get("tenantId") or "").lower()
                external = not m.get("fromMe") and not (own_tid and tid == own_tid)
                await self.fs.connectors.teams(user_id, {
                    "chat_id": cid, "message_id": mid, "kind": "message",
                    "from": str(m.get("from") or ""), "from_id": from_id,
                    "from_address": str(sender.get("email") or ""), "from_me": bool(m.get("fromMe")),
                    "text": str(m.get("text") or ""), "at": str(m.get("at") or ""),
                    "chat_type": str(chat.get("type") or ""), "topic": str(chat.get("topic") or ""),
                    "members": roster, "external": external,
                })
                kept += 1
            nxt = str(res.data.get("next") or "")
            if done or not nxt or nxt == before:
                break
            before = nxt
        return kept

    async def _catch_up(self, user_id: int) -> None:
        """DMs already waiting on a reply get the same nudge as Slack (added by the Teams reply task)."""
        try:
            from mavis.attention import teams_replies
        except ImportError:
            return
        try:
            await teams_replies.catch_up(self.fs.provider, user_id)
        except Exception:  # noqa: BLE001 - the nudge is an extra; the first sync must finish
            log.warning("first_sync.teams_catch_up_failed", user_id=user_id)

    # --- OneDrive --------------------------------------------------------------------------------------

    async def onedrive(self, user_id: int) -> list[str]:
        own = await self._data(user_id, "onedrive.list_recent", {"max_results": DRIVE_MAX})
        shared = await self._data(user_id, "onedrive.list_recent",
                                  {"shared_with_me": True, "max_results": DRIVE_MAX})
        mine, theirs = self._titles(own), self._titles(shared)
        if not (mine or theirs):
            return []
        parts = []
        if mine:
            parts.append("OneDrive files the user keeps: " + ", ".join(mine))
        if theirs:
            parts.append("shared with the user: " + ", ".join(theirs))
        await self.fs.memory.learn(user_id, guard.redact("; ".join(parts)), f"onedrive:{user_id}:titles")
        return [f"Found {len(mine) + len(theirs)} OneDrive files"]

    @staticmethod
    def _titles(data: Any) -> list[str]:
        if data is None:
            return []
        names = [" ".join(str(f.get("name") or "").split())[:TITLE_CAP]
                 for f in extract_list(data, "files", "data.files") if isinstance(f, dict)]
        return [n for n in names if n][:TITLES_PER_LIST]
