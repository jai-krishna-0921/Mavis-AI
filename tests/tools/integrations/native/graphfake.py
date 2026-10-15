"""A fake Microsoft Graph over httpx.MockTransport, a scripted TokenSource and Graph-shaped builders."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired

DEFAULT_ACCOUNT = {"upn": "me@orbit.test", "mail": "me@orbit.test", "tid": "tenant-1", "oid": "oid-me"}


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
    (`/v1.0/me/messages`); an optional `query={"$filter": "..."}` also requires those decoded query
    parameters (exact values, subset match).

    Like real Graph, a 200 JSON answer only carries on-request fields: `internetMessageHeaders` needs
    `$select` naming it, chat `members` / `lastMessagePreview` need `$expand` naming them, and
    `Prefer: outlook.body-content-type="text"` turns html message bodies into text. Builders always
    build the full object; the fake strips what the request did not ask for. Other `Prefer` values
    (timezone, maxpagesize) are not honoured: assert them with `query_of` and `request.headers`.

    A request no route answers is recorded in `unrouted` and answered 404 `UnroutedInFakeGraph`
    (distinct from a real `itemNotFound`); call `assert_all_routed()` at the end of a test."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Any, dict[str, str] | None]] = []
        self.unrouted: list[httpx.Request] = []
        self.requests: list[httpx.Request] = []
        self.active = self.peak = 0

    def on(
        self, method: str, pattern: str, response: Any, *, query: dict[str, str] | None = None
    ) -> FakeGraph:
        self.routes.append((method, re.compile(pattern), response, query))
        return self

    def assert_all_routed(self) -> None:
        assert not self.unrouted, "unrouted requests: " + ", ".join(
            f"{r.method} {r.url.path}" for r in self.unrouted
        )

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            for method, pattern, response, query in self.routes:
                if request.method == method and pattern.search(request.url.path) and _has(request, query):
                    if isinstance(response, list):
                        answer = response.pop(0) if len(response) > 1 else response[0]
                    else:
                        answer = response
                    return _shape(request, answer(request) if callable(answer) else answer)
            self.unrouted.append(request)
            return graph_error(
                404, "UnroutedInFakeGraph", f"no fake route for {request.method} {request.url.path}"
            )
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

    def calls(self, method: str, pattern: str, *, query: dict[str, str] | None = None) -> list[httpx.Request]:
        return [
            r for r in self.requests
            if r.method == method and re.search(pattern, r.url.path) and _has(r, query)
        ]


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def query_of(request: httpx.Request) -> dict[str, str]:
    """The decoded query (`$filter`, `$search`, `$select`, ...). A repeated key keeps its last value."""
    return dict(request.url.params.multi_items())


def _has(request: httpx.Request, query: dict[str, str] | None) -> bool:
    if not query:
        return True
    got = query_of(request)
    return all(got.get(k) == v for k, v in query.items())


def _names(request: httpx.Request, key: str) -> set[str]:
    return {part.split("(")[0].strip() for part in query_of(request).get(key, "").split(",") if part.strip()}


def _strip(node: Any, select: set[str], expand: set[str], text_body: bool) -> None:
    if isinstance(node, list):
        for child in node:
            _strip(child, select, expand, text_body)
    elif isinstance(node, dict):
        if "internetMessageHeaders" in node and "internetMessageHeaders" not in select:
            del node["internetMessageHeaders"]
        if "chatType" in node:
            for field in ("members", "lastMessagePreview"):
                if field in node and field not in expand:
                    del node[field]
        body = node.get("body")
        is_html = isinstance(body, dict) and body.get("contentType") == "html"
        if text_body and "conversationId" in node and is_html:
            node["body"] = {"contentType": "text", "content": re.sub(r"<[^>]+>", "", body["content"])}
        for child in node.values():
            _strip(child, select, expand, text_body)


def _shape(request: httpx.Request, answer: httpx.Response) -> httpx.Response:
    """Drop on-request fields the caller did not ask for from a 200 JSON answer, as Graph does."""
    if answer.status_code != 200 or "json" not in answer.headers.get("content-type", ""):
        return answer
    try:
        data = json.loads(answer.content)
    except ValueError:
        return answer
    text_body = 'outlook.body-content-type="text"' in request.headers.get("Prefer", "")
    _strip(data, _names(request, "$select"), _names(request, "$expand"), text_body)
    return httpx.Response(200, json=data, headers={k: v for k, v in answer.headers.items()
                                                   if k.lower() not in ("content-length", "content-type")})


def page(items: list, next_link: str | None = None, delta_link: str | None = None) -> dict:
    out: dict[str, Any] = {"value": items}
    if next_link:
        out["@odata.nextLink"] = next_link
    if delta_link:
        out["@odata.deltaLink"] = delta_link
    return out


def graph_removed(item_id: str, reason: str = "deleted") -> dict:
    """A delta tombstone."""
    return {"id": item_id, "@removed": {"reason": reason}}


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
        "toRecipients": [{"emailAddress": {"name": to, "address": to}}],
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
    attendees: tuple[str | tuple[str, str, str], ...] = (),
    organizer: str = "Alice <alice@acme.com>",
    is_online: bool = False,
    cancelled: bool = False,
    response: str = "accepted",
    all_day: bool = False,
    modified: str = "2026-10-01T08:00:00Z",
    show_as: str = "busy",
    organizer_is_me: bool = False,
    series_master: str | None = None,
    location: str = "",
) -> dict:
    """An attendee is an address string or a (name, address, response) triple; `organizer_is_me` makes the
    signed-in user (me@orbit.test) the organiser."""
    if organizer_is_me:
        organizer = "Me <me@orbit.test>"

    def guest(spec: str | tuple[str, str, str]) -> dict:
        if isinstance(spec, tuple):
            name, address, state = spec
            who = {"name": name, "address": address}
        else:
            who, state = _address(spec), "none"
        return {"emailAddress": who, "status": {"response": state}, "type": "required"}

    def stamp(value: str) -> dict:
        if "T" not in value:
            value += "T00:00:00"
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
        "showAs": show_as,
        "location": {"displayName": location},
        "organizer": {"emailAddress": _address(organizer)},
        "isOrganizer": organizer_is_me,
        "attendees": [guest(a) for a in attendees],
        "isOnlineMeeting": is_online,
        "onlineMeeting": (
            {"joinUrl": f"https://teams.microsoft.com/l/meetup-join/{eid}"} if is_online else None
        ),
        "responseStatus": {"response": response, "time": "2026-10-01T08:00:00Z"},
        "webLink": f"https://outlook.office.com/calendar/item/{eid}",
        "lastModifiedDateTime": modified,
        "type": "occurrence" if series_master else "singleInstance",
        "seriesMasterId": series_master,
        "iCalUId": f"ical-{eid}",
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
    me: tuple[str, str, str] = ("Me", "me@orbit.test", "oid-me"),
    with_members: bool = True,
) -> dict:
    """`me` is the signed-in user (name, email, user id); Graph lists them among the members too."""
    members = (*members, *(() if any(m[2] == me[2] for m in members) else (me,)))
    names = {uid: name for name, _, uid in members}
    chat = {
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
            "body": {"contentType": "html" if last_text.startswith("<") else "text", "content": last_text},
            "from": {"user": {"id": last_from, "displayName": names.get(last_from, ""),
                              "userIdentityType": "aadUser"}},
        },
    }
    if not with_members:
        del chat["members"]
    return chat


def graph_chat_message(
    mid: str,
    chat: str,
    *,
    from_id: str | None = "u-alice",
    name: str | None = "Alice",
    text: str = "hi",
    at: str = "2026-10-05T10:00:00Z",
    kind: str = "message",
    html: bool = False,
    deleted: bool = False,
    mentions: tuple[str, ...] = (),
) -> dict:
    """`from_id=None` builds an app/bot message (no user author); `mentions` are mentioned user ids."""
    return {
        "id": mid,
        "chatId": chat,
        "messageType": kind,
        "createdDateTime": at,
        "lastModifiedDateTime": at,
        "deletedDateTime": at if deleted else None,
        "importance": "normal",
        "webUrl": f"https://teams.microsoft.com/l/message/{chat}/{mid}",
        "body": {"contentType": "html" if html else "text", "content": text},
        "from": None if kind != "message" or from_id is None
        else {"user": {"id": from_id, "displayName": name, "userIdentityType": "aadUser"}},
        "attachments": [],
        "mentions": [{"id": i, "mentionText": "x", "mentioned": {"user": {"id": uid}}}
                     for i, uid in enumerate(mentions)],
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
    folder: bool = False,
    child_count: int = 3,
    drive_id: str = "d-me",
    remote_drive_id: str = "d-remote",
) -> dict:
    facet: dict[str, Any] = (
        {"folder": {"childCount": child_count}} if folder else {"file": {"mimeType": mime}}
    )
    item: dict[str, Any] = {
        "id": iid,
        "name": name,
        "size": size,
        **facet,
        "parentReference": {"driveId": drive_id, "id": "root-id", "path": "/drive/root:"},
        "createdDateTime": modified,
        "lastModifiedDateTime": modified,
        "lastModifiedBy": {"user": {"displayName": "Alice", "email": "alice@acme.com"}},
        "webUrl": web_url or f"https://acme-my.sharepoint.com/personal/x/{iid}",
    }
    if shared_by:
        # a shared item is read from the OWNER's drive: /drives/{remote driveId}/items/{remote id}
        item["remoteItem"] = {
            "id": f"remote-{iid}",
            "name": name,
            "size": size,
            **facet,
            "parentReference": {"driveId": remote_drive_id, "id": "remote-root"},
            "webUrl": item["webUrl"],
            "shared": {"owner": {"user": {"displayName": shared_by}}, "sharedDateTime": modified},
        }
    return item


__all__ = [
    "FakeGraph", "GraphTokens", "body_of", "graph_chat", "graph_chat_message", "graph_error",
    "graph_event", "graph_item", "graph_message", "graph_removed", "page", "query_of",
]
