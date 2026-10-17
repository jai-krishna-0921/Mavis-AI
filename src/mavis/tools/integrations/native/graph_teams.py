# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Teams chats over Microsoft Graph: list chats, read a chat, send a plain-text message.

Chat message bodies are HTML and everything in them is third-party data: they go through the hardened
`html_to_text`, are capped, and system events, deleted messages and app messages are dropped. Sending is
not idempotent, so a lost answer is UNCONFIRMED (GraphClient.raw) and nothing here repeats it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from mavis.domain.errors import FailureKind
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.graph import _SCOPE_DETAIL as SCOPE_DETAIL
from mavis.tools.integrations.native.graph import GRAPH, GraphError, seg, with_ms
from mavis.tools.integrations.native.graph_html import html_to_text, offload

TEXT_CAP = 2000
TRUNCATED = "...[truncated]"
NO_TEAMS = "Teams is not available for this Microsoft account"
CHAT_TYPES = ("oneOnOne", "group", "meeting")
LIST_TOP = 50  # the unordered fallback reads a full page, then sorts and cuts locally
MEMBER_FETCH_CAP = 20
MEMBER_CONCURRENCY = 5
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?(Z|[+-]\d{2}:\d{2})")


def _text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if not isinstance(content, str):
        return ""
    text = html_to_text(content) if str(body.get("contentType")).lower() == "html" else content.strip()
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP].rstrip() + TRUNCATED


def _mentions(mention: dict, oid: str) -> bool:
    mentioned = mention.get("mentioned")
    user = mentioned.get("user") if isinstance(mentioned, dict) else None
    return isinstance(user, dict) and str(user.get("id") or "") == oid


def message_view(raw: dict, chat_id: str, me_oid: str, me_mail: str) -> dict | None:
    """One chat message, or None for a system event, a deleted message or one with no user author."""
    if raw.get("messageType") != "message" or raw.get("deletedDateTime"):
        return None
    author = (raw.get("from") or {}).get("user") if isinstance(raw.get("from"), dict) else None
    if not isinstance(author, dict) or not author.get("id"):
        return None
    from_id = str(author["id"])
    mentions = [m for m in raw.get("mentions") or [] if isinstance(m, dict)]
    return {
        "id": str(raw.get("id") or ""),
        "chatId": with_ms(chat_id),
        "from": str(author.get("displayName") or ""),
        "fromId": from_id,
        "fromMe": bool(me_oid) and from_id == me_oid,
        "text": _text(raw.get("body")),
        "at": str(raw.get("createdDateTime") or ""),
        "mentionsMe": bool(me_oid) and any(_mentions(m, me_oid) for m in mentions),
    }


def _member(raw: dict, me_oid: str) -> dict:
    uid = str(raw.get("userId") or "")
    return {"id": uid, "name": str(raw.get("displayName") or ""), "email": str(raw.get("email") or ""),
            "tenantId": str(raw.get("tenantId") or ""),
            "me": bool(me_oid) and uid == me_oid}


def chat_view(raw: dict, me_oid: str, me_mail: str = "") -> dict:
    cid = str(raw.get("id") or "")
    kind = raw.get("chatType")
    preview = raw.get("lastMessagePreview") if isinstance(raw.get("lastMessagePreview"), dict) else None
    last = message_view(preview, cid, me_oid, me_mail) if preview else None
    return {
        "id": with_ms(cid),
        "type": kind if kind in CHAT_TYPES else "group",
        "topic": str(raw.get("topic") or ""),
        "members": [_member(m, me_oid) for m in raw.get("members") or [] if isinstance(m, dict)],
        "lastMessage": last,
        "lastMessageAt": str((preview or {}).get("createdDateTime") or raw.get("lastUpdatedDateTime") or ""),
        "webUrl": str(raw.get("webUrl") or ""),
    }


class TeamsActions:
    def __init__(self, ex: Any) -> None:
        self.ex = ex

    async def _me(self, uid: int) -> tuple[str, str]:
        acct = await self.ex._tokens.account(uid, NativeProvider.MICROSOFT) or {}
        return str(acct.get("oid") or ""), str(acct.get("mail") or "").lower()

    @staticmethod
    def _unavailable(exc: GraphError) -> GraphError:
        if exc.kind is FailureKind.PERMISSION_MISSING and SCOPE_DETAIL not in exc.detail:
            return GraphError(FailureKind.PERMISSION_MISSING, NO_TEAMS)
        return exc

    async def _members(self, uid: int, chat_id: str, me_oid: str) -> list[dict]:
        raw, _, _ = await self.ex.graph.pages(
            uid, f"{GRAPH}/chats/{seg(chat_id)}/members", {"$top": "50"}, 50,
            on_forbidden=FailureKind.PERMISSION_MISSING)
        return [_member(m, me_oid) for m in raw]

    async def chats(self, uid: int, a: Any) -> dict:
        me_oid, me_mail = await self._me(uid)
        steps = chat_list_steps(a.limit)
        raw: list[dict] = []
        try:
            for n, params in enumerate(steps):
                try:
                    raw, _, _ = await self.ex.graph.pages(
                        uid, f"{GRAPH}/me/chats", params, a.limit if n < 2 else LIST_TOP, max_pages=1,
                        on_forbidden=FailureKind.PERMISSION_MISSING)
                    break
                except GraphError as exc:
                    if exc.kind is not FailureKind.INVALID_ARGUMENT or n == len(steps) - 1:
                        raise
            # the HTML parse waits on its worker process: not on the event loop
            chats = await offload(lambda: [chat_view(c, me_oid, me_mail) for c in raw])
            if n >= 1:
                await self._fill_members(uid, chats, raw, me_oid)
            if n == 2:
                chats.sort(key=lambda c: c["lastMessageAt"], reverse=True)
                chats = chats[:a.limit]
        except GraphError as exc:
            if exc.kind is FailureKind.NOT_FOUND:  # a personal account answers 404 for chats
                raise GraphError(FailureKind.PERMISSION_MISSING, NO_TEAMS) from None
            raise self._unavailable(exc) from None
        out: dict[str, Any] = {"chats": chats}
        if n == 2:
            out["unordered"] = True  # a plain page: chats outside it were not seen (the poll keeps its floor)
        return out

    async def _fill_members(self, uid: int, chats: list[dict], raw: list[dict], me_oid: str) -> None:
        gate = asyncio.Semaphore(MEMBER_CONCURRENCY)
        todo = [(c, r) for c, r in zip(chats, raw, strict=True) if not r.get("members")][:MEMBER_FETCH_CAP]

        async def fill(chat: dict, source: dict) -> None:
            async with gate:
                chat["members"] = await self._members(uid, str(source.get("id") or ""), me_oid)

        await asyncio.gather(*(fill(c, r) for c, r in todo))

    async def chat(self, uid: int, a: Any) -> dict:
        me_oid, me_mail = await self._me(uid)
        try:
            raw = await self.ex.graph.json(
                uid, "GET", f"{GRAPH}/chats/{seg(a.chat_id)}", params={"$expand": "members"},
                on_forbidden=FailureKind.PERMISSION_MISSING)
        except GraphError as exc:
            raise self._unavailable(exc) from None
        return await offload(chat_view, raw if isinstance(raw, dict) else {}, me_oid, me_mail)

    async def history(self, uid: int, a: Any) -> dict:
        me_oid, me_mail = await self._me(uid)
        params = {"$top": str(a.limit), "$orderby": "createdDateTime desc"}
        if a.before:
            if not _ISO.fullmatch(a.before):
                raise GraphError(FailureKind.INVALID_ARGUMENT, "before must be an ISO time", field="before")
            params["$filter"] = f"createdDateTime lt {a.before}"
        try:
            raw, _, _ = await self.ex.graph.pages(
                uid, f"{GRAPH}/chats/{seg(a.chat_id)}/messages", params, a.limit, max_pages=3,
                on_forbidden=FailureKind.PERMISSION_MISSING)
        except GraphError as exc:
            raise self._unavailable(exc) from None
        views = await offload(lambda: [message_view(m, a.chat_id, me_oid, me_mail) for m in raw])
        messages = [v for v in views if v is not None]
        full = len(raw) >= a.limit
        oldest = min((str(m.get("createdDateTime") or "") for m in raw), default="") if full else ""
        return {"messages": messages, "skipped": len(raw) - len(messages), "next": oldest or None}

    async def send(self, uid: int, a: Any) -> dict:
        try:
            made = await self.ex.graph.json(
                uid, "POST", f"{GRAPH}/chats/{seg(a.chat_id)}/messages",
                body={"body": {"contentType": "text", "content": a.text}},
                on_forbidden=FailureKind.PERMISSION_MISSING)
        except GraphError as exc:
            raise self._unavailable(exc) from None
        made = made if isinstance(made, dict) else {}
        return {"messageId": str(made.get("id") or ""), "chatId": with_ms(a.chat_id),
                "at": str(made.get("createdDateTime") or "")}


def chat_list_steps(limit: int) -> tuple[dict, dict, dict]:
    """The three /me/chats requests `chats` tries in order (the live check probes the same ones)."""
    orderby = "lastMessagePreview/createdDateTime desc"
    return (
        {"$expand": "members,lastMessagePreview", "$orderby": orderby, "$top": str(limit)},
        {"$expand": "lastMessagePreview", "$orderby": orderby, "$top": str(limit)},
        {"$expand": "lastMessagePreview", "$top": str(LIST_TOP)},
    )


def teams_handlers(ex: Any) -> dict[str, Callable[[int, Any], Awaitable[Any]]]:
    t = TeamsActions(ex)
    return {"teams.chats": t.chats, "teams.history": t.history, "teams.send": t.send, "teams.chat": t.chat}
