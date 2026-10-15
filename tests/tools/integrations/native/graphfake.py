"""A fake Microsoft Graph over httpx.MockTransport, a scripted TokenSource and Graph-shaped builders."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired

DEFAULT_ACCOUNT = {"upn": "me@orbit.test", "tid": "t-1", "oid": "u-me"}


class GraphTokens:
    """Scripted TokenSource: records the `force` flag of every call, raises ReauthRequired when revoked."""

    def __init__(self, account_data: dict | None = DEFAULT_ACCOUNT, *, revoked: bool = False) -> None:
        self.calls: list[bool] = []
        self.account_data, self.revoked = account_data, revoked

    async def access_token(self, user_id: int, provider: NativeProvider, *, force: bool = False) -> str:
        assert provider is NativeProvider.MICROSOFT
        if self.revoked:
            raise ReauthRequired("invalid_grant")
        self.calls.append(force)
        return "tok-forced" if force else "tok"

    async def account(self, user_id: int, provider: NativeProvider) -> dict | None:
        return self.account_data


def graph_error(
    status: int, code: str = "generalException", message: str = "boom", *, headers: dict | None = None
) -> httpx.Response:
    body = {"error": {"code": code, "message": message, "innerError": {"request-id": "r-1"}}}
    return httpx.Response(status, json=body, headers=headers)


class FakeGraph:
    """Routes (method, path regex) to a response or a callable. Requests are recorded.

    A list is a script: one answer per call, the last one repeats. Patterns match the URL path
    (`/v1.0/me/messages`), not the query string."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Any]] = []
        self.requests: list[httpx.Request] = []
        self.active = self.peak = 0

    def on(self, method: str, pattern: str, response: Any) -> FakeGraph:
        self.routes.append((method, re.compile(pattern), response))
        return self

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            for method, pattern, response in self.routes:
                if request.method == method and pattern.search(request.url.path):
                    if isinstance(response, list):
                        answer = response.pop(0) if len(response) > 1 else response[0]
                    else:
                        answer = response
                    return answer(request) if callable(answer) else answer
            return graph_error(404, "itemNotFound", f"no fake route for {request.method} {request.url.path}")
        finally:
            self.active -= 1

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def executor(self, tokens: GraphTokens | None = None):
        """(MicrosoftExecutor, recorded sleeps). Imported lazily: the executor lands after this fake."""
        from mavis.tools.integrations.native.microsoft import MicrosoftExecutor

        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        return MicrosoftExecutor(tokens or GraphTokens(), self.client(), sleep=sleep), sleeps

    def calls(self, method: str, pattern: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and re.search(pattern, r.url.path)]


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def page(items: list, next_link: str | None = None, delta_link: str | None = None) -> dict:
    out: dict[str, Any] = {"value": items}
    if next_link:
        out["@odata.nextLink"] = next_link
    if delta_link:
        out["@odata.deltaLink"] = delta_link
    return out


def _address(spec: str) -> dict:
    """'Alice <alice@acme.com>' or 'alice@acme.com' -> Graph emailAddress."""
    m = re.fullmatch(r"\s*(.*?)\s*<([^>]+)>\s*", spec)
    name, addr = (m[1].strip('"'), m[2]) if m else ("", spec.strip())
    return {"name": name, "address": addr}


def graph_message(
    mid: str,
    *,
    thread: str = "c1",
    sender: str = "Alice <alice@acme.com>",
    to: str = "me@orbit.test",
    subject: str = "Hello",
    text: str = "Body text",
    html: str | None = None,
    unread: bool = True,
    importance: str = "normal",
    classification: str = "focused",
    folder: str = "inbox",
    sent_at: str = "2026-10-05T10:00:00Z",
    categories: tuple[str, ...] = (),
    has_attachments: bool = False,
    auth: str = "dkim=pass; dmarc=pass",
) -> dict:
    who = {"emailAddress": _address(sender)}
    return {
        "id": mid,
        "conversationId": thread,
        "subject": subject,
        "bodyPreview": text[:100],
        "body": {"contentType": "html" if html else "text", "content": html or text},
        "from": who,
        "sender": who,
        "toRecipients": [{"emailAddress": {"address": to}}],
        "ccRecipients": [],
        "receivedDateTime": sent_at,
        "sentDateTime": sent_at,
        "isRead": not unread,
        "isDraft": False,
        "importance": importance,
        "inferenceClassification": classification,
        "categories": list(categories),
        "hasAttachments": has_attachments,
        "parentFolderId": folder,
        "webLink": f"https://outlook.office.com/m/{mid}",
        "internetMessageHeaders": [
            {"name": "Authentication-Results", "value": auth},
            {"name": "Received", "value": "from mx.acme.com"},
        ],
    }


def graph_event(
    eid: str,
    *,
    subject: str = "Meeting",
    start: str = "2026-10-06T09:00:00",
    end: str = "2026-10-06T09:30:00",
    tz: str = "UTC",
    attendees: tuple[str, ...] = (),
    organizer: str = "Alice <alice@acme.com>",
    is_online: bool = False,
    cancelled: bool = False,
    response: str = "accepted",
    all_day: bool = False,
) -> dict:
    def stamp(value: str) -> dict:
        # Graph pads to seven fractional digits and states the zone separately
        return {"dateTime": value if "." in value else f"{value}.0000000", "timeZone": tz}

    return {
        "id": eid,
        "subject": subject,
        "bodyPreview": "",
        "start": stamp(start),
        "end": stamp(end),
        "isAllDay": all_day,
        "isCancelled": cancelled,
        "showAs": "busy",
        "location": {"displayName": ""},
        "organizer": {"emailAddress": _address(organizer)},
        "attendees": [
            {"emailAddress": _address(a), "status": {"response": "none"}, "type": "required"}
            for a in attendees
        ],
        "isOnlineMeeting": is_online,
        "onlineMeeting": (
            {"joinUrl": f"https://teams.microsoft.com/l/meetup-join/{eid}"} if is_online else None
        ),
        "responseStatus": {"response": response, "time": "2026-10-01T08:00:00Z"},
        "webLink": f"https://outlook.office.com/calendar/item/{eid}",
    }


def graph_chat(
    cid: str,
    *,
    chat_type: str = "oneOnOne",
    topic: str | None = None,
    members: tuple[tuple[str, str, str], ...] = (("Alice", "alice@acme.com", "u-alice"),),
    last_text: str = "hi",
    last_from: str = "u-alice",
    last_at: str = "2026-10-05T10:00:00Z",
) -> dict:
    names = {uid: name for name, _, uid in members}
    return {
        "id": cid,
        "chatType": chat_type,
        "topic": topic,
        "createdDateTime": "2026-09-01T09:00:00Z",
        "lastUpdatedDateTime": last_at,
        "webUrl": f"https://teams.microsoft.com/l/chat/{cid}",
        "members": [
            {"id": f"mem-{uid}", "displayName": name, "userId": uid, "email": email,
             "roles": ["owner"], "@odata.type": "#microsoft.graph.aadUserConversationMember"}
            for name, email, uid in members
        ],
        "lastMessagePreview": {
            "id": f"{cid}-last",
            "createdDateTime": last_at,
            "messageType": "message",
            "body": {"contentType": "text", "content": last_text},
            "from": {"user": {"id": last_from, "displayName": names.get(last_from, ""),
                              "userIdentityType": "aadUser"}},
        },
    }


def graph_chat_message(
    mid: str,
    chat: str,
    *,
    from_id: str = "u-alice",
    name: str = "Alice",
    text: str = "hi",
    at: str = "2026-10-05T10:00:00Z",
    kind: str = "message",
    html: bool = False,
) -> dict:
    return {
        "id": mid,
        "chatId": chat,
        "messageType": kind,
        "createdDateTime": at,
        "lastModifiedDateTime": at,
        "deletedDateTime": None,
        "importance": "normal",
        "webUrl": f"https://teams.microsoft.com/l/message/{chat}/{mid}",
        "body": {"contentType": "html" if html else "text", "content": text},
        "from": None if kind != "message"
        else {"user": {"id": from_id, "displayName": name, "userIdentityType": "aadUser"}},
        "attachments": [],
        "mentions": [],
    }


def graph_item(
    iid: str,
    *,
    name: str = "notes.txt",
    size: int = 1000,
    mime: str = "text/plain",
    modified: str = "2026-10-05T10:00:00Z",
    shared_by: str | None = None,
    web_url: str | None = None,
) -> dict:
    item: dict[str, Any] = {
        "id": iid,
        "name": name,
        "size": size,
        "file": {"mimeType": mime},
        "createdDateTime": modified,
        "lastModifiedDateTime": modified,
        "lastModifiedBy": {"user": {"displayName": "Alice", "email": "alice@acme.com"}},
        "webUrl": web_url or f"https://acme-my.sharepoint.com/personal/x/{iid}",
    }
    if shared_by:
        item["remoteItem"] = {
            "id": f"remote-{iid}",
            "shared": {"owner": {"user": {"displayName": shared_by}}, "sharedDateTime": modified},
        }
    return item


__all__ = [
    "FakeGraph", "GraphTokens", "body_of", "graph_chat", "graph_chat_message", "graph_error",
    "graph_event", "graph_item", "graph_message", "page",
]
