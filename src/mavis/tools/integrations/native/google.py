"""Mavis AI's in-house Google Workspace executor (Gmail, Calendar, Drive, Docs, Sheets, People).

Implements the provider-neutral action names in actions.py over the Google REST APIs with a short-lived
access token from a TokenSource. Result `data` uses the keys normalize.py, mail_render.py and
workspace_render.py already read, so the poller, intake and renderers work unchanged. Everything a
Google API returns is third-party data. Error text on a failed ToolResult is vendor detail for logs and
the model only; the user-facing words come from the FailureKind.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ValidationError

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ToolResult, UserRef
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.native import gmail_mime
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired, TokenSource

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
PEOPLE = "https://people.googleapis.com/v1"

MAX_JSON_BYTES = 16 * 1024 * 1024
DOWNLOAD_CAP_BYTES = 1_000_000  # exported or downloaded file text is cut here; larger files are flagged
SEARCH_BODY_CAP = 2000  # body chars kept per message in a list (the full text is mail.read)
THREAD_BODY_CAP = 8000
FETCH_CONCURRENCY = 5
MAX_PAGES = 10
MAX_RETRY_WAIT_S = 10.0
DEFAULT_RETRY_WAIT_S = 1.0
SERVER_BACKOFF_S = 1.0
MIN_FREE_MINUTES = 15
FIND_WINDOW_BACK = timedelta(days=30)
FIND_WINDOW_AHEAD = timedelta(days=365)
MAX_SHEET_ROWS = 200
MAX_CONTACT_PAGES = 5

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_APPS = "application/vnd.google-apps."
EXPORT_DEFAULTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",  # the first sheet only
    "application/vnd.google-apps.presentation": "text/plain",
}
TEXT_MIMES = ("text/", "application/json", "application/xml", "application/x-ndjson")
FILE_FIELDS = (
    "nextPageToken,files(id,name,mimeType,modifiedTime,sharedWithMeTime,"
    "owners(displayName,emailAddress,me),sharingUser(displayName,emailAddress))"
)
META_FIELDS = (
    "id,name,mimeType,size,modifiedTime,createdTime,ownedByMe,shared,trashed,"
    "owners(displayName,emailAddress,me)"
)
PERMISSION_FIELDS = "nextPageToken,permissions(id,type,role,emailAddress,domain,displayName,deleted)"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers"

RATE_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded", "rate_limit_exceeded"})
SCOPE_REASONS = frozenset({"insufficientpermissions", "access_token_scope_insufficient", "autherror"})
DISABLED_REASONS = frozenset({"accessnotconfigured", "service_disabled"})
TOO_LARGE_REASONS = frozenset({"exportsizelimitexceeded", "filenotdownloadable"})
# Vendor parameter names Google reports a 400 against, mapped to our argument names.
VENDOR_FIELDS = {
    "timemin": "time_min", "timemax": "time_max", "updatedmin": "updated_min", "q": "query",
    "maxresults": "max_results", "pagesize": "max_results", "attendees": "attendees",
    "summary": "summary", "description": "description", "start": "start", "end": "duration_minutes",
    "mimetype": "mime_type", "range": "range", "query": "query",
}
# Retry safety. A method that only reads is idempotent; any other call must declare idempotent=True to be
# repeated after a 5xx or a read error. A failure before the request can have left the machine is always safe.
SAFE_METHODS = frozenset({"GET", "HEAD"})
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
UNCONFIRMED_DETAIL = (
    "google outcome unknown: {what}. The request may or may not have been processed, so do NOT repeat it "
    "blindly. Tell the user it may have gone through and to check (for mail, the Sent folder; for a "
    "calendar event, the calendar) before trying again."
)
ID_ARGS = ("message_id", "thread_id", "event_id", "file_id", "document_id", "spreadsheet_id")

# Gmail search: operators that name who or what a message is about (a targeted search), and the boxes and
# categories a listing leaves out unless the query itself names them.
TARGETING_OPS = frozenset({
    "from", "to", "cc", "bcc", "subject", "list", "deliveredto", "rfc822msgid", "filename", "replyto",
})
MAILBOXES = ("spam", "trash")
CATEGORIES = ("promotions", "social", "forums")
_CONNECTORS = frozenset({"or", "and", "around", "|"})
_TOKEN = re.compile(r"""[-+]?[({]*\w+:"[^"]*"|"[^"]*"|\S+""")
_OPERATOR = re.compile(r"""^(?P<op>[A-Za-z_][\w]*):(?P<val>.*)$""", re.S)


class GoogleError(Exception):
    def __init__(self, kind: FailureKind, detail: str, field: str | None = None) -> None:
        super().__init__(detail)
        self.kind, self.detail, self.field = kind, detail, field


def search_query(query: str) -> str:
    """The Gmail query with structural exclusions. Spam and trash are always left out unless the query
    names them (or in:anywhere). Promotions, social and forums are left out of a listing (a query with no
    sender, recipient, subject or free-text term) unless the query names that category, so sync and
    "what's new" never read bulk mail, while a search for a named sender still finds theirs."""
    targeted, mentioned = False, set()
    for raw in _TOKEN.findall(query):
        token = raw.lstrip("-+({").rstrip(")}")
        negated = raw.startswith("-")
        if not token or token.lower() in _CONNECTORS:
            continue
        m = _OPERATOR.match(token)
        if m is None:
            targeted = targeted or not negated
            continue
        op, val = m["op"].lower(), m["val"].strip('"').lower()
        if op in TARGETING_OPS:
            targeted = targeted or not negated
        elif op in ("category", "in", "label", "is"):
            mentioned.add(val)
    extra = []
    if "anywhere" not in mentioned:
        extra += [f"-in:{box}" for box in MAILBOXES if box not in mentioned]
    if not targeted:
        extra += [f"-category:{c}" for c in CATEGORIES if c not in mentioned]
    return " ".join(part for part in (query.strip(), *extra) if part)


def _q(value: str) -> str:
    return quote(value, safe="")


def _rfc3339(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat(timespec="seconds")


def _when(dt: datetime) -> dict[str, str]:
    """A Calendar start/end: a named zone keeps its wall clock, an offset is explicit, naive means UTC."""
    if isinstance(dt.tzinfo, ZoneInfo):
        return {"dateTime": dt.replace(tzinfo=None).isoformat(timespec="seconds"), "timeZone": dt.tzinfo.key}
    if dt.tzinfo is None:
        return {"dateTime": dt.isoformat(timespec="seconds"), "timeZone": "UTC"}
    return {"dateTime": dt.isoformat(timespec="seconds")}


def _parse_when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None


def _error_info(body: bytes) -> tuple[str, str, str | None]:
    """(reason, message, vendor field) from a Google error body; empty strings when it is not JSON."""
    try:
        err = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return "", body[:200].decode("utf-8", "replace"), None
    if not isinstance(err, dict):
        return "", str(err)[:200], None
    first = (err.get("errors") or [{}])[0] if isinstance(err.get("errors"), list) else {}
    reason = str(first.get("reason") or "")
    field = first.get("location") if isinstance(first.get("location"), str) else None
    for d in err.get("details") or []:
        if not isinstance(d, dict):
            continue
        reason = reason or str(d.get("reason") or "")
        for v in d.get("fieldViolations") or []:
            if isinstance(v, dict) and v.get("field"):
                field = field or str(v["field"])
    return reason, str(err.get("message") or err.get("status") or ""), field


class GoogleExecutor:
    provider = NativeProvider.GOOGLE

    def __init__(
        self, tokens: TokenSource, client: httpx.AsyncClient,
        *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._tokens, self._client, self._sleep = tokens, client, sleep
        self._warmed: set[int] = set()
        self._handlers: dict[str, Callable[[int, Any], Awaitable[Any]]] = {
            "mail.search": self._mail_search, "mail.read": self._mail_read, "mail.thread": self._mail_thread,
            "mail.draft": self._mail_draft, "mail.send": self._mail_send, "mail.reply": self._mail_reply,
            "mail.profile": self._mail_profile,
            "calendar.list": self._calendar_list, "calendar.find": self._calendar_find,
            "calendar.free_slots": self._calendar_free_slots, "calendar.create_event": self._calendar_create,
            "calendar.update_event": self._calendar_update,
            "drive.search": self._drive_search, "drive.list_recent": self._drive_recent,
            "drive.read": self._drive_text, "drive.download": self._drive_download,
            "drive.meta": self._drive_meta, "drive.permissions": self._drive_permissions,
            "docs.read": self._docs_read, "sheets.find": self._sheets_find, "sheets.read": self._sheets_read,
            "contacts.search": self._contacts_search, "contacts.list": self._contacts_list,
        }

    def handles(self, action: str) -> bool:
        return action in self._handlers

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        handler = self._handlers.get(action)
        if handler is None:
            return ToolResult(ok=False, error=f"google does not implement {action!r}",
                              error_kind=FailureKind.UNKNOWN)
        model = ACTIONS[action].args_model
        try:
            parsed = model.model_validate(args)
        except ValidationError as exc:
            locs = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
            return ToolResult(ok=False, error=f"invalid arguments for {action}: {', '.join(locs)}",
                              error_kind=FailureKind.INVALID_ARGUMENT, error_field=locs[0] if locs else None)
        try:
            return ToolResult(ok=True, data=await handler(user.user_id, parsed))
        except ReauthRequired as exc:
            return ToolResult(ok=False, error=f"google grant unusable: {exc}", error_kind=FailureKind.AUTH)
        except GoogleError as exc:
            field = self._our_field(model, exc)
            return ToolResult(ok=False, error=exc.detail[:300], error_kind=exc.kind, error_field=field)

    @staticmethod
    def _our_field(model: type[BaseModel], exc: GoogleError) -> str | None:
        fields = model.model_fields
        if exc.kind is FailureKind.NOT_FOUND:
            return next((n for n in ID_ARGS if n in fields), None)
        if exc.kind is FailureKind.INVALID_ARGUMENT and exc.field:
            segment = re.split(r"[.\[]", exc.field, maxsplit=1)[0]
            mapped = VENDOR_FIELDS.get(segment.lower(), segment)
            return mapped if mapped in fields else None
        return None

    # --- transport ---------------------------------------------------------------------------------

    async def _send(
        self, token: str, method: str, url: str, params: dict | None, body: Any, cap: int
    ) -> tuple[int, httpx.Headers, bytes, bool]:
        async with self._client.stream(
            method, url, params=params, json=body, headers={"Authorization": f"Bearer {token}"}
        ) as resp:
            chunks, size, cut = [], 0, False
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > cap:
                    cut = True
                    break
            return resp.status_code, resp.headers, b"".join(chunks)[:cap], cut

    async def _raw(
        self, uid: int, method: str, url: str, *, params: dict | None = None, body: Any = None,
        cap: int = MAX_JSON_BYTES, idempotent: bool | None = None,
    ) -> tuple[httpx.Headers, bytes, bool]:
        """One authorised call with the retry rules. `idempotent` is declared by the call (None: the method's
        own default, true for GET and HEAD only). A 401 refreshes the token once and a rate limit waits once
        (the request was rejected before it ran, so any call may repeat). A connect failure or a pool timeout
        means the request never left, so any call may repeat. A 5xx or a read or write error may come after
        Google acted: only an idempotent call repeats, and a non-idempotent one ends as UNCONFIRMED (the
        caller and the user are told it may have gone through)."""
        if idempotent is None:
            idempotent = method.upper() in SAFE_METHODS
        token = await self._tokens.access_token(uid, NativeProvider.GOOGLE)
        refreshed = throttled = backed_off = False
        while True:
            try:
                status, headers, data, cut = await self._send(token, method, url, params, body, cap)
            except httpx.TransportError as exc:
                never_sent = isinstance(exc, NEVER_SENT)
                if not (never_sent or idempotent):
                    raise GoogleError(FailureKind.UNCONFIRMED, UNCONFIRMED_DETAIL.format(
                        what=f"the connection failed after the request was sent ({type(exc).__name__})")
                    ) from None
                if backed_off:
                    raise GoogleError(FailureKind.UNAVAILABLE,
                                      f"google unreachable ({type(exc).__name__})") from None
                backed_off = True
                await self._sleep(SERVER_BACKOFF_S)
                continue
            if status < 300:
                return headers, data, cut
            reason, message, field = _error_info(data)
            detail = f"google {status} {reason}: {message}".strip()
            key = reason.lower()
            if status == 401:
                if refreshed:
                    raise GoogleError(FailureKind.AUTH, detail)
                token = await self._tokens.access_token(uid, NativeProvider.GOOGLE, force=True)
                refreshed = True
                continue
            if status == 429 or (status == 403 and key in RATE_REASONS):
                wait = _retry_after(headers)
                wait = DEFAULT_RETRY_WAIT_S if wait is None else wait
                if throttled or wait > MAX_RETRY_WAIT_S:
                    raise GoogleError(FailureKind.RATE_LIMITED, detail)
                throttled = True
                await self._sleep(wait)
                continue
            if status >= 500:
                if not idempotent:
                    raise GoogleError(FailureKind.UNCONFIRMED, UNCONFIRMED_DETAIL.format(what=detail))
                if backed_off:
                    raise GoogleError(FailureKind.UNAVAILABLE, detail)
                backed_off = True
                await self._sleep(SERVER_BACKOFF_S)
                continue
            raise self._classify(status, key, detail, field)

    @staticmethod
    def _classify(status: int, reason: str, detail: str, field: str | None) -> GoogleError:
        if status == 400:
            return GoogleError(FailureKind.INVALID_ARGUMENT, detail, field)
        if status == 404:
            return GoogleError(FailureKind.NOT_FOUND, detail)
        if status == 403:
            if reason in SCOPE_REASONS:
                return GoogleError(FailureKind.AUTH, detail)
            if reason in DISABLED_REASONS:
                return GoogleError(FailureKind.UNAVAILABLE, detail)
            if reason in TOO_LARGE_REASONS:
                return GoogleError(FailureKind.INVALID_ARGUMENT, detail)
            return GoogleError(FailureKind.NOT_FOUND, detail)  # no access to that item: as good as absent
        if status in (409, 412, 422):
            return GoogleError(FailureKind.INVALID_ARGUMENT, detail, field)
        return GoogleError(FailureKind.UNKNOWN, detail)

    async def _json(
        self, uid: int, method: str, url: str, *, params: dict | None = None, body: Any = None,
        idempotent: bool | None = None,
    ) -> Any:
        _, data, cut = await self._raw(uid, method, url, params=params, body=body, idempotent=idempotent)
        if cut:
            raise GoogleError(FailureKind.UNKNOWN, "google response larger than the size limit")
        if not data.strip():
            return {}
        try:
            return json.loads(data)
        except ValueError:
            raise GoogleError(FailureKind.UNKNOWN, "google returned a body that is not JSON") from None

    async def _pages(
        self, uid: int, url: str, params: dict, key: str, limit: int, page_param: str
    ) -> tuple[list[dict], str | None, dict]:
        """Follow nextPageToken until `limit` items. Returns (items, next token if more, last page)."""
        items: list[dict] = []
        token: str | None = None
        page: dict = {}
        for _ in range(MAX_PAGES):
            page = await self._json(uid, "GET", url, params={
                **params, page_param: min(limit - len(items), params.get(page_param, limit)), **(
                    {"pageToken": token} if token else {})})
            items += [x for x in page.get(key) or [] if isinstance(x, dict)]
            token = page.get("nextPageToken")
            if len(items) >= limit or not token:
                break
        return items[:limit], token, page

    async def _account_email(self, uid: int) -> str | None:
        account = await self._tokens.account(uid, NativeProvider.GOOGLE)
        email = (account or {}).get("email")
        return str(email) if email else None

    # --- Gmail -------------------------------------------------------------------------------------

    @staticmethod
    def _message(raw: dict, cap: int) -> dict[str, Any]:
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        text, truncated = gmail_mime.body_text(payload, cap)
        snippet = html.unescape(str(raw.get("snippet") or ""))
        head = lambda name: gmail_mime.header(payload, name)  # noqa: E731
        return {
            "messageId": raw.get("id", ""), "threadId": raw.get("threadId", ""),
            "sender": head("from"), "to": head("to"), "cc": head("cc"), "subject": head("subject"),
            "date": head("date"), "rfc822MessageId": head("message-id"),
            "messageText": text or snippet, "bodyTruncated": truncated, "snippet": snippet,
            "labelIds": list(raw.get("labelIds") or []), "messageTimestamp": raw.get("internalDate"),
            "attachments": gmail_mime.attachments(payload),
            "payload": {"headers": gmail_mime.kept_headers(payload)},
        }

    async def _mail_search(self, uid: int, a: Any) -> dict:
        ids, _, page = await self._pages(
            uid, f"{GMAIL}/messages", {"q": search_query(a.query), "maxResults": 100}, "messages",
            a.max_results, "maxResults")
        gate = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(mid: str) -> dict | None:
            async with gate:
                try:
                    return await self._json(
                        uid, "GET", f"{GMAIL}/messages/{_q(mid)}", params={"format": "full"})
                except GoogleError as exc:
                    if exc.kind is FailureKind.NOT_FOUND:
                        return None  # deleted between the list and the read
                    raise

        got = await asyncio.gather(*(one(str(i["id"])) for i in ids), return_exceptions=True)
        for item in got:
            if isinstance(item, BaseException):
                raise item
        out: dict[str, Any] = {
            "messages": [self._message(r, SEARCH_BODY_CAP) for r in got if r],
            "resultSizeEstimate": page.get("resultSizeEstimate", len(ids)),
        }
        return out

    async def _mail_read(self, uid: int, a: Any) -> dict:
        raw = await self._json(uid, "GET", f"{GMAIL}/messages/{_q(a.message_id)}", params={"format": "full"})
        return self._message(raw, gmail_mime.BODY_CAP)

    async def _mail_thread(self, uid: int, a: Any) -> dict:
        raw = await self._json(uid, "GET", f"{GMAIL}/threads/{_q(a.thread_id)}", params={"format": "full"})
        return {"threadId": raw.get("id", a.thread_id),
                "messages": [self._message(m, THREAD_BODY_CAP) for m in raw.get("messages") or []]}

    async def _mail_profile(self, uid: int, a: Any) -> dict:
        return await self._json(uid, "GET", f"{GMAIL}/profile")

    async def _mail_draft(self, uid: int, a: Any) -> dict:
        raw = await self._raw_message(uid, a.to, a.cc, a.subject, a.body)
        made = await self._json(uid, "POST", f"{GMAIL}/drafts", body={"message": {"raw": raw}})
        message = made.get("message") or {}
        return {"draftId": made.get("id", ""), "id": made.get("id", ""),
                "messageId": message.get("id", ""), "threadId": message.get("threadId", "")}

    async def _mail_send(self, uid: int, a: Any) -> dict:
        raw = await self._raw_message(uid, a.to, a.cc, a.subject, a.body)
        sent = await self._json(uid, "POST", f"{GMAIL}/messages/send", body={"raw": raw})
        return {"messageId": sent.get("id", ""), "id": sent.get("id", ""),
                "threadId": sent.get("threadId", ""), "labelIds": sent.get("labelIds", [])}

    async def _mail_reply(self, uid: int, a: Any) -> dict:
        thread = await self._json(
            uid, "GET", f"{GMAIL}/threads/{_q(a.thread_id)}",
            params={"format": "metadata", "metadataHeaders": ["Message-ID", "References", "Subject"]})
        messages = [m for m in thread.get("messages") or [] if isinstance(m, dict)]
        if not messages:
            raise GoogleError(FailureKind.NOT_FOUND, "google thread has no messages")
        last = max(messages, key=lambda m: int(m.get("internalDate") or 0))
        payload = last.get("payload") or {}
        raw = await self._raw_message(
            uid, [a.to], [], gmail_mime.reply_subject(gmail_mime.header(payload, "subject")), a.body,
            in_reply_to=gmail_mime.header(payload, "message-id"),
            references=gmail_mime.header(payload, "references"))
        sent = await self._json(uid, "POST", f"{GMAIL}/messages/send",
                                body={"raw": raw, "threadId": a.thread_id})
        return {"messageId": sent.get("id", ""), "id": sent.get("id", ""),
                "threadId": sent.get("threadId", a.thread_id), "labelIds": sent.get("labelIds", [])}

    async def _raw_message(
        self, uid: int, to: list[str], cc: list[str], subject: str, body: str, **reply: str
    ) -> str:
        try:
            msg = gmail_mime.build_message(
                sender=await self._account_email(uid), to=to, cc=cc, subject=subject, body=body, **reply)
            return gmail_mime.encode_raw(msg)
        except ValueError as exc:
            raise GoogleError(FailureKind.INVALID_ARGUMENT, f"message not valid: {exc}") from None

    # --- Calendar ----------------------------------------------------------------------------------

    async def _events(self, uid: int, params: dict, limit: int) -> dict:
        items, token, page = await self._pages(
            uid, f"{CALENDAR}/calendars/primary/events",
            {"singleEvents": "true", "orderBy": "startTime", "maxResults": 250, **params}, "items",
            limit, "maxResults")
        out: dict[str, Any] = {"items": items, "timeZone": page.get("timeZone", "")}
        if token:
            out["nextPageToken"] = token
        return out

    async def _calendar_list(self, uid: int, a: Any) -> dict:
        params: dict[str, Any] = {"timeMin": _rfc3339(a.time_min), "timeMax": _rfc3339(a.time_max)}
        if a.updated_min is not None:
            params["updatedMin"] = _rfc3339(a.updated_min)
            params["showDeleted"] = "true"  # a cancelled event is a change the poller must see
        return await self._events(uid, params, a.max_results)

    async def _calendar_find(self, uid: int, a: Any) -> dict:
        now = datetime.now(UTC)
        return await self._events(uid, {
            "q": a.query, "timeMin": _rfc3339(now - FIND_WINDOW_BACK),
            "timeMax": _rfc3339(now + FIND_WINDOW_AHEAD),
        }, 25)

    async def _calendar_free_slots(self, uid: int, a: Any) -> dict:
        start, end = a.time_min, a.time_max
        # a POST that only reads, so it declares itself repeatable
        fb = await self._json(uid, "POST", f"{CALENDAR}/freeBusy", idempotent=True, body={
            "timeMin": _rfc3339(start), "timeMax": _rfc3339(end), "items": [{"id": "primary"}]})
        calendars = fb.get("calendars") or {}
        busy_raw = ((calendars.get("primary") or next(iter(calendars.values()), {})) or {}).get("busy") or []
        lo = start if start.tzinfo else start.replace(tzinfo=UTC)
        hi = end if end.tzinfo else end.replace(tzinfo=UTC)
        spans = sorted((max(_parse_when(b["start"]), lo), min(_parse_when(b["end"]), hi)) for b in busy_raw)
        free, cursor = [], lo
        for s, e in spans:
            if s > cursor:
                free.append((cursor, s))
            cursor = max(cursor, e)
        if cursor < hi:
            free.append((cursor, hi))
        zone = lo.tzinfo

        def show(d: datetime) -> str:
            return d.astimezone(zone).isoformat(timespec="seconds")

        return {
            "time_min": show(lo), "time_max": show(hi),
            "busy": [{"start": show(s), "end": show(e)} for s, e in spans],
            "free_slots": [
                {"start": show(s), "end": show(e), "minutes": int((e - s).total_seconds() // 60)}
                for s, e in free if (e - s) >= timedelta(minutes=MIN_FREE_MINUTES)
            ],
        }

    async def _calendar_create(self, uid: int, a: Any) -> dict:
        body: dict[str, Any] = {
            "summary": a.summary, "start": _when(a.start),
            "end": _when(a.start + timedelta(minutes=a.duration_minutes)),
        }
        if a.description:
            body["description"] = a.description
        if a.attendees:
            body["attendees"] = [{"email": e} for e in a.attendees]
        params = {"sendUpdates": "all"} if a.attendees else None
        return await self._json(uid, "POST", f"{CALENDAR}/calendars/primary/events", params=params, body=body)

    async def _calendar_update(self, uid: int, a: Any) -> dict:
        url = f"{CALENDAR}/calendars/primary/events/{_q(a.event_id)}"
        body: dict[str, Any] = {}
        if a.summary is not None:
            body["summary"] = a.summary
        if a.start is not None and a.duration_minutes is not None:
            body["start"] = _when(a.start)
            body["end"] = _when(a.start + timedelta(minutes=a.duration_minutes))
        if a.description is not None:
            body["description"] = a.description
        if a.attendees is not None:
            existing = {
                str(g.get("email", "")).lower(): g
                for g in (await self._json(uid, "GET", url)).get("attendees") or [] if isinstance(g, dict)
            }
            body["attendees"] = [existing.get(e.lower(), {"email": e}) for e in a.attendees]
        params = {"sendUpdates": "all"} if a.attendees else None
        # setting the same fields twice is harmless, but a repeat would notify the attendees twice
        return await self._json(uid, "PATCH", url, params=params, body=body, idempotent=params is None)

    # --- Drive, Docs, Sheets -----------------------------------------------------------------------

    @staticmethod
    def _drive_q(query: str, *, extra: str = "") -> tuple[str, bool]:
        """(q, ordered): the user's Drive query ANDed with trashed = false. Drive refuses orderBy together
        with fullText terms."""
        clauses = [f"({query.strip()})"] if query.strip() else []
        clauses += [extra] if extra else []
        clauses.append("trashed = false")
        return " and ".join(clauses), not re.search(r"\bfulltext\b", query, re.I)

    async def _files(self, uid: int, q: str, order: str | None, limit: int) -> dict:
        params: dict[str, Any] = {
            "q": q, "pageSize": limit, "fields": FILE_FIELDS,
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
        }
        if order:
            params["orderBy"] = order
        files, token, _ = await self._pages(uid, f"{DRIVE}/files", params, "files", limit, "pageSize")
        out: dict[str, Any] = {"files": files}
        if token:
            out["nextPageToken"] = token
        return out

    async def _drive_search(self, uid: int, a: Any) -> dict:
        q, ordered = self._drive_q(a.query)
        return await self._files(uid, q, "modifiedTime desc" if ordered else None, a.max_results)

    async def _drive_recent(self, uid: int, a: Any) -> dict:
        if a.shared_with_me:
            return await self._files(uid, "sharedWithMe and trashed = false", "sharedWithMeTime desc",
                                     a.max_results)
        return await self._files(uid, "trashed = false", "modifiedTime desc", a.max_results)

    async def _meta(self, uid: int, file_id: str) -> dict:
        return await self._json(uid, "GET", f"{DRIVE}/files/{_q(file_id)}",
                                params={"fields": META_FIELDS, "supportsAllDrives": "true"})

    async def _drive_meta(self, uid: int, a: Any) -> dict:
        return await self._meta(uid, a.file_id)

    async def _drive_permissions(self, uid: int, a: Any) -> dict:
        perms, _, _ = await self._pages(
            uid, f"{DRIVE}/files/{_q(a.file_id)}/permissions",
            {"fields": PERMISSION_FIELDS, "supportsAllDrives": "true", "pageSize": 100}, "permissions",
            500, "pageSize")
        return {"permissions": perms}

    async def _file_text(self, uid: int, file_id: str, export_as: str = "") -> dict:
        meta = await self._meta(uid, file_id)
        mime = str(meta.get("mimeType") or "")
        base = f"{DRIVE}/files/{_q(file_id)}"
        if mime.startswith(GOOGLE_APPS):
            target = export_as or EXPORT_DEFAULTS.get(mime, "")
            if not target:
                raise GoogleError(FailureKind.INVALID_ARGUMENT, f"google file type {mime} has no text export",
                                  "mime_type")
            url, params = f"{base}/export", {"mimeType": target}
        elif mime.startswith(TEXT_MIMES):
            url, params = base, {"alt": "media", "supportsAllDrives": "true"}
        else:
            raise GoogleError(FailureKind.INVALID_ARGUMENT,
                              f"{mime or 'this file'} is not a text file", "file_id")
        headers, data, cut = await self._raw(uid, "GET", url, params=params, cap=DOWNLOAD_CAP_BYTES)
        charset = None
        match = re.search(r"charset=([\w-]+)", headers.get("content-type", ""))
        if match:
            charset = match[1]
        return {
            "file_id": file_id, "id": file_id, "name": meta.get("name", ""), "mimeType": mime,
            "size": meta.get("size"), "text": gmail_mime.decode_bytes(data, charset), "truncated": cut,
        }

    async def _drive_text(self, uid: int, a: Any) -> dict:
        return await self._file_text(uid, a.file_id)

    async def _drive_download(self, uid: int, a: Any) -> dict:
        return await self._file_text(uid, a.file_id, a.mime_type)

    async def _docs_read(self, uid: int, a: Any) -> dict:
        return await self._json(uid, "GET", f"{DOCS}/{_q(a.document_id)}",
                                params={"fields": "documentId,title,body"})

    async def _sheets_find(self, uid: int, a: Any) -> dict:
        q, ordered = self._drive_q(a.query, extra=f"mimeType = '{SHEET_MIME}'")
        found = await self._files(uid, q, "modifiedTime desc" if ordered else None, a.max_results)
        return {"spreadsheets": found["files"]}

    async def _sheets_read(self, uid: int, a: Any) -> dict:
        target = a.range.strip()
        if not target:
            meta = await self._json(uid, "GET", f"{SHEETS}/{_q(a.spreadsheet_id)}",
                                    params={"fields": "sheets.properties.title"})
            sheets = meta.get("sheets") or []
            title = ((sheets[0] if sheets else {}).get("properties") or {}).get("title")
            if not title:
                raise GoogleError(FailureKind.NOT_FOUND, "google spreadsheet has no sheets")
            target = "'" + str(title).replace("'", "''") + "'"
        got = await self._json(uid, "GET", f"{SHEETS}/{_q(a.spreadsheet_id)}/values/{_q(target)}",
                               params={"valueRenderOption": "FORMATTED_VALUE"})
        got["values"] = (got.get("values") or [])[:MAX_SHEET_ROWS]
        return {"valueRanges": [got]}

    # --- People ------------------------------------------------------------------------------------

    async def _contacts_search(self, uid: int, a: Any) -> dict:
        if uid not in self._warmed:  # Google: the first searchContacts call must be an empty-query warm-up
            await self._json(uid, "GET", f"{PEOPLE}/people:searchContacts",
                             params={"query": "", "readMask": "names"})
            self._warmed.add(uid)
        got = await self._json(uid, "GET", f"{PEOPLE}/people:searchContacts", params={
            "query": a.query, "pageSize": a.max_results, "readMask": PERSON_FIELDS})
        return {"results": got.get("results") or []}

    async def _contacts_list(self, uid: int, a: Any) -> dict:
        people, token = [], None
        for _ in range(MAX_CONTACT_PAGES):
            params = {"personFields": "names,emailAddresses", "pageSize": 1000}
            if token:
                params["pageToken"] = token
            page = await self._json(uid, "GET", f"{PEOPLE}/people/me/connections", params=params)
            people += [p for p in page.get("connections") or [] if isinstance(p, dict)]
            token = page.get("nextPageToken")
            if not token:
                break
        return {"connections": people}
