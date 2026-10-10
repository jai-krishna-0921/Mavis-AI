"""Mavis AI's in-house Google Workspace executor (Gmail, Calendar, Drive, Docs, Sheets, People).

Implements the provider-neutral action names in actions.py over the Google REST APIs with a short-lived
access token from a TokenSource. Result `data` uses the keys normalize.py, mail_render.py and
workspace_render.py already read, so the poller, intake and renderers work unchanged. Everything a
Google API returns is third-party data. Error text on a failed ToolResult is vendor detail for logs and
the model only; the user-facing words come from the FailureKind.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ValidationError

from mavis.config import get_settings
from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ToolResult, UserRef
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.composio_map import input_option
from mavis.tools.integrations.native import gmail_mime
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired, TokenSource
from mavis.tools.integrations.native.docs_markdown import MAX_MARKDOWN_CHARS, markdown_requests
from mavis.tools.integrations.native.slides_outline import parse_outline, slide_requests

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
PEOPLE = "https://people.googleapis.com/v1"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TASKS = "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks"  # the user's default list
MEET = "https://meet.googleapis.com/v2"
SLIDES = "https://slides.googleapis.com/v1/presentations"
FORMS = "https://forms.googleapis.com/v1/forms"
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CAP_BYTES = 5 * 1024 * 1024  # one multipart request; larger artifacts are refused, never truncated
_MIME = re.compile(r"^[A-Za-z0-9][\w.+-]*/[A-Za-z0-9][\w.+-]*$")
_A1_ONLY = re.compile(r"^\$?[A-Za-z]{1,3}\$?\d*(?::\$?[A-Za-z]{1,3}\$?\d*)?$")

MAX_JSON_BYTES = 16 * 1024 * 1024
DOWNLOAD_CAP_BYTES = 1_000_000  # exported or downloaded file text is cut here; larger files are flagged
EXPORT_CAP_BYTES = 10_000_000  # Drive's own export limit: an exported file sent to the user as an attachment
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
ID_ARGS = ("message_id", "thread_id", "event_id", "file_id", "document_id", "spreadsheet_id", "task_id",
           "conference_record_id", "calendar_id", "presentation_id", "form_id", "message_ids", "thread_ids")
MAX_TRANSCRIPT_ENTRIES = 400
MAX_FORM_RESPONSES_PAGES = 5
MAX_MEET_PARTICIPANTS = 30
SYSTEM_LABEL_LOCKED = frozenset({"TRASH", "DRAFT", "SENT"})  # trash has its own, approved, action

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


def _balanced(query: str) -> bool:
    """Quotes close and brackets nest. Anything else would let the exclusions we append land inside a
    phrase or a group, where they no longer apply."""
    stack: list[str] = []
    in_quote = False
    for ch in query:
        if ch == '"':
            in_quote = not in_quote
        elif in_quote:
            continue
        elif ch in "({":
            stack.append(")" if ch == "(" else "}")
        elif ch in ")}":
            if not stack or stack.pop() != ch:
                return False
    return not in_quote and not stack


def search_query(query: str) -> str:
    """The Gmail query with structural exclusions. Spam and trash are always left out unless the query
    names them (or in:anywhere). Promotions, social and forums are left out of a listing (a query with no
    sender, recipient, subject or free-text term) unless the query names that category, so sync and
    "what's new" never read bulk mail, while a search for a named sender still finds theirs.

    The exclusions are ANDed onto the whole query, so the query must not be able to escape them: quotes and
    brackets must balance (else INVALID_ARGUMENT), a dangling OR or minus at the end is dropped, and a query
    that uses OR or braces is wrapped in parentheses so no alternative sits outside the exclusions."""
    query = query.strip()
    if not _balanced(query):
        raise GoogleError(FailureKind.INVALID_ARGUMENT,
                          "search query has an unclosed quote or bracket", "query")
    tokens = _TOKEN.findall(query)
    while tokens and (tokens[-1].lower() in _CONNECTORS or tokens[-1] in ("-", "+")):
        query = query[: query.rindex(tokens.pop())].rstrip()
    targeted, mentioned, grouped = False, set(), False
    for raw in tokens:
        token = raw.lstrip("-+({").rstrip(")}")
        negated = raw.startswith("-")
        grouped = grouped or token.lower() in ("or", "|") or "{" in raw or "|" in raw
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
    if grouped and query:
        query = f"({query})"
    return " ".join(part for part in (query, *extra) if part)


def _drive_quote(value: str) -> str:
    """A Drive query string literal: backslash and single quote escaped, nothing else can end it."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


_DRIVE_TOKEN = re.compile(r"""\s*(?:(?P<str>'(?:[^'\\]|\\.)*')|(?P<op><=|>=|!=|=|<|>)|(?P<par>[()])"""
                          r"""|(?P<word>[A-Za-z_][A-Za-z0-9_]*))""")
_DRIVE_TEXT_FIELDS = {"name": ("contains", "=", "!="), "fulltext": ("contains",),
                      "mimetype": ("contains", "=", "!=")}
_DRIVE_TIME_FIELDS = {"modifiedtime": "modifiedTime", "createdtime": "createdTime",
                      "viewedbymetime": "viewedByMeTime", "sharedwithmetime": "sharedWithMeTime"}
_DRIVE_CANONICAL = {"name": "name", "fulltext": "fullText", "mimetype": "mimeType"}
_DRIVE_PEOPLE = {"parents", "owners", "writers", "readers"}
_DRIVE_TIME_VALUE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9:.]+(?:Z|[+-][0-9:]+)?)?$")
_DRIVE_MAX_DEPTH = 8
_DRIVE_MAX_TERMS = 30


class _DriveParser:
    """Recursive descent over the part of Drive's query language we allow:

        expr  := and ("or" and)*          and := not ("and" not)*        not := "not" not | atom
        atom  := "(" expr ")" | "sharedWithMe" | 'id' "in" (parents|owners|writers|readers)
               | (name|fullText|mimeType) (contains|=|!=) 'text'
               | (modifiedTime|createdTime|viewedByMeTime|sharedWithMeTime) (=|!=|<|<=|>|>=) 'date'
               | starred (=|!=) true|false

    parse() returns (query rebuilt from the tree with every group parenthesised, uses_fulltext) or None when
    the text is not in the grammar. `trashed` is deliberately absent: the caller always adds it."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.t, self.i, self.fulltext, self.terms = tokens, 0, False, 0

    @classmethod
    def parse(cls, text: str) -> tuple[str, bool] | None:
        tokens: list[tuple[str, str]] = []
        pos = 0
        while pos < len(text):
            m = _DRIVE_TOKEN.match(text, pos)
            if m is None or m.end() == pos:
                return None if text[pos:].strip() else cls._done(tokens)
            kind = m.lastgroup or ""
            tokens.append((kind, m[kind]))
            pos = m.end()
        return cls._done(tokens)

    @classmethod
    def _done(cls, tokens: list[tuple[str, str]]) -> tuple[str, bool] | None:
        p = cls(tokens)
        try:
            out = p._expr(0)
        except _NotDrive:
            return None
        return (out, p.fulltext) if p.i == len(tokens) else None

    def _peek(self) -> tuple[str, str]:
        return self.t[self.i] if self.i < len(self.t) else ("", "")

    def _take(self, kind: str, value: str | None = None) -> str:
        k, v = self._peek()
        if k != kind or (value is not None and v.lower() != value):
            raise _NotDrive
        self.i += 1
        return v

    def _is_word(self, word: str) -> bool:
        k, v = self._peek()
        return k == "word" and v.lower() == word

    def _expr(self, depth: int) -> str:
        if depth > _DRIVE_MAX_DEPTH:
            raise _NotDrive
        parts = [self._and(depth)]
        while self._is_word("or"):
            self.i += 1
            parts.append(self._and(depth))
        return " or ".join(parts) if len(parts) > 1 else parts[0]

    def _and(self, depth: int) -> str:
        parts = [self._not(depth)]
        while self._is_word("and"):
            self.i += 1
            parts.append(self._not(depth))
        return " and ".join(parts) if len(parts) > 1 else parts[0]

    def _not(self, depth: int) -> str:
        if self._is_word("not"):
            self.i += 1
            return f"not {self._not(depth + 1)}"
        return self._atom(depth)

    def _string(self) -> str:
        raw = self._take("str")[1:-1]
        return re.sub(r"\\(.)", r"\1", raw, flags=re.S)

    def _atom(self, depth: int) -> str:
        self.terms += 1
        if self.terms > _DRIVE_MAX_TERMS:
            raise _NotDrive
        kind, value = self._peek()
        if kind == "par" and value == "(":
            self.i += 1
            inner = self._expr(depth + 1)
            self._take("par", ")")
            return f"({inner})"
        if kind == "str":
            ident = self._string()
            self._take("word", "in")
            who = self._take("word").lower()
            if who not in _DRIVE_PEOPLE:
                raise _NotDrive
            return f"{_drive_quote(ident)} in {who}"
        if kind != "word":
            raise _NotDrive
        self.i += 1
        field = value.lower()
        if field == "sharedwithme":
            return "sharedWithMe"
        if field in _DRIVE_TEXT_FIELDS:
            op = self._peek()[1].lower()
            if self._peek()[0] not in ("op", "word") or op not in _DRIVE_TEXT_FIELDS[field]:
                raise _NotDrive
            self.i += 1
            self.fulltext = self.fulltext or field == "fulltext"
            return f"{_DRIVE_CANONICAL[field]} {op} {_drive_quote(self._string())}"
        if field in _DRIVE_TIME_FIELDS:
            op = self._take("op")
            when = self._string()
            if op not in ("=", "!=", "<", "<=", ">", ">=") or not _DRIVE_TIME_VALUE.match(when):
                raise _NotDrive
            return f"{_DRIVE_TIME_FIELDS[field]} {op} '{when}'"
        if field == "starred":
            op = self._take("op")
            flag = self._take("word").lower()
            if op not in ("=", "!=") or flag not in ("true", "false"):
                raise _NotDrive
            return f"starred {op} {flag}"
        raise _NotDrive


class _NotDrive(Exception):
    """The text is not in the allowed Drive grammar."""



def _bad_segment(value: str, *, allow_slash: bool = False) -> bool:
    """Would this value, used as one URL path segment, change which resource is addressed? Empty, a dot
    segment, a slash (or backslash, which some servers fold into one) or a control character would."""
    if value in ("", ".", "..") or value != value.strip():
        return True
    return any(ch == "\\" or (ch == "/" and not allow_slash) or ord(ch) < 32 or ord(ch) == 127
               for ch in value)


def _q(value: str, *, allow_slash: bool = False) -> str:
    """Percent-encode one path segment. Refuses what _bad_segment names, so a model-supplied id can never
    reach another endpoint."""
    if _bad_segment(value, allow_slash=allow_slash):
        raise GoogleError(FailureKind.INVALID_ARGUMENT, "id is not a valid identifier")
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


def _cell(value: Any) -> dict:
    """One Sheets CellData: numbers stay numbers and text stays text. A leading = is never a formula here
    (like input_option's RAW): text from a file or email must never become =IMAGE(...)."""
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, int | float):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": "" if value is None else str(value)}}


class GoogleExecutor:
    provider = NativeProvider.GOOGLE

    def __init__(
        self, tokens: TokenSource, client: httpx.AsyncClient,
        *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._tokens, self._client, self._sleep = tokens, client, sleep
        self._warmed: set[tuple[int, str]] = set()
        self._handlers: dict[str, Callable[[int, Any], Awaitable[Any]]] = {
            "mail.search": self._mail_search, "mail.read": self._mail_read, "mail.thread": self._mail_thread,
            "mail.draft": self._mail_draft, "mail.send": self._mail_send, "mail.reply": self._mail_reply,
            "mail.profile": self._mail_profile,
            "calendar.list": self._calendar_list, "calendar.find": self._calendar_find,
            "calendar.free_slots": self._calendar_free_slots, "calendar.create_event": self._calendar_create,
            "calendar.update_event": self._calendar_update,
            "drive.search": self._drive_search, "drive.list_recent": self._drive_recent,
            "drive.read": self._drive_text, "drive.download": self._drive_download,
            "drive.export_file": self._drive_export_file,
            "drive.meta": self._drive_meta, "drive.permissions": self._drive_permissions,
            "docs.read": self._docs_read, "sheets.find": self._sheets_find, "sheets.read": self._sheets_read,
            "contacts.search": self._contacts_search, "contacts.list": self._contacts_list,
            # Workspace writes. docs.append, drive.upload, tasks.complete and tasks.update are not here:
            # workspace_tools composes them from the primitives below (read, then insert_text; stage the
            # artifact, then upload_file; tasks.get, then tasks.patch), so every risk check runs first.
            "drive.create_folder": self._drive_create_folder, "drive.move": self._drive_move,
            "drive.share": self._drive_share, "drive.upload_file": self._drive_upload_file,
            "docs.create": self._docs_create, "docs.insert_text": self._docs_insert_text,
            "docs.comment": self._docs_comment, "sheets.create": self._sheets_create,
            "sheets.append_row": self._sheets_append_row, "sheets.update_range": self._sheets_update_range,
            "tasks.list": self._tasks_list, "tasks.get": self._tasks_get, "tasks.add": self._tasks_add,
            "tasks.patch": self._tasks_patch, "tasks.delete": self._tasks_delete,
            "meet.create": self._meet_create, "meet.transcript": self._meet_transcript,
            "meet.recent": self._meet_recent,
            "mail.archive": self._mail_archive, "mail.mark_read": self._mail_mark_read,
            "mail.mark_unread": self._mail_mark_unread, "mail.label": self._mail_label,
            "mail.trash": self._mail_trash, "mail.untrash": self._mail_untrash,
            "calendar.calendars": self._calendar_calendars, "calendar.get": self._calendar_get,
            "calendar.delete_event": self._calendar_delete, "calendar.respond": self._calendar_respond,
            "contacts.create": self._contacts_create, "contacts.update": self._contacts_update,
            "slides.read": self._slides_read, "slides.create": self._slides_create,
            "forms.read": self._forms_read, "forms.responses": self._forms_responses,
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
        for name in ID_ARGS:  # an id becomes a URL path segment: refuse one that could address something else
            value = getattr(parsed, name, None)
            bad = (any(_bad_segment(v) for v in value) if isinstance(value, list)
                   else isinstance(value, str) and _bad_segment(value))
            if bad:
                return ToolResult(ok=False, error=f"invalid arguments for {action}: {name}",
                                  error_kind=FailureKind.INVALID_ARGUMENT, error_field=name)
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
        self, token: str, method: str, url: str, params: dict | None, body: Any, cap: int,
        raw: tuple[bytes, str] | None = None,
    ) -> tuple[int, httpx.Headers, bytes, bool]:
        headers = {"Authorization": f"Bearer {token}"}
        if raw is not None:  # (bytes, content type): an upload, instead of a JSON body
            headers["Content-Type"] = raw[1]
        async with self._client.stream(
            method, url, params=params, headers=headers,
            **({"content": raw[0]} if raw is not None else {"json": body}),
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
        cap: int = MAX_JSON_BYTES, idempotent: bool | None = None, raw: tuple[bytes, str] | None = None,
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
                status, headers, data, cut = await self._send(token, method, url, params, body, cap, raw)
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
        idempotent: bool | None = None, raw: tuple[bytes, str] | None = None,
    ) -> Any:
        _, data, cut = await self._raw(uid, method, url, params=params, body=body, idempotent=idempotent,
                                       raw=raw)
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

    # --- Gmail organising (gmail.modify) -------------------------------------------------------------
    # Label changes and trash/untrash set a state, so repeating one after an unknown outcome is safe
    # (idempotent=True). Creating a label is not (a repeat conflicts), so it is declared False.

    async def _labels(self, uid: int) -> list[dict]:
        got = await self._json(uid, "GET", f"{GMAIL}/labels")
        return [x for x in got.get("labels") or [] if isinstance(x, dict)]

    async def _label_ids(self, uid: int, names: list[str], *, create: bool) -> list[str]:
        """Label ids for names (or ids). A name that does not exist is created when `create`, else refused."""
        if not names:
            return []
        known = await self._labels(uid)
        by_key = {str(x.get("name", "")).casefold(): str(x["id"]) for x in known if x.get("id")}
        by_key.update({str(x["id"]).casefold(): str(x["id"]) for x in known if x.get("id")})
        out: list[str] = []
        for name in names:
            found = by_key.get(name.strip().casefold())
            if found is None and create:
                made = await self._json(uid, "POST", f"{GMAIL}/labels", idempotent=False, body={
                    "name": name.strip(), "labelListVisibility": "labelShow",
                    "messageListVisibility": "show"})
                found = str(made.get("id") or "")
            if not found:
                raise GoogleError(FailureKind.INVALID_ARGUMENT, f"there is no Gmail label named {name!r}",
                                  "remove" if not create else "add")
            out.append(found)
        return list(dict.fromkeys(out))

    async def _modify_mail(
        self, uid: int, a: Any, verb: str, add: list[str], remove: list[str]
    ) -> dict:
        """Add and remove label ids on a.message_ids (one batchModify per 1000) and a.thread_ids (one
        threads.modify each, a few at a time)."""
        body = {"addLabelIds": add, "removeLabelIds": remove}
        if a.message_ids:
            await self._json(uid, "POST", f"{GMAIL}/messages/batchModify", idempotent=True,
                             body={"ids": list(a.message_ids), **body})
        await self._each(a.thread_ids, lambda t: self._json(
            uid, "POST", f"{GMAIL}/threads/{_q(t)}/modify", idempotent=True, body=body))
        return {"verb": verb, "messages": len(a.message_ids), "threads": len(a.thread_ids),
                "added": add, "removed": remove}

    @staticmethod
    async def _each(items: list[str], call: Callable[[str], Awaitable[Any]]) -> None:
        gate = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(item: str) -> None:
            async with gate:
                await call(item)

        results = await asyncio.gather(*(one(i) for i in items), return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r

    async def _mail_archive(self, uid: int, a: Any) -> dict:
        return await self._modify_mail(uid, a, "Archived", [], ["INBOX"])

    async def _mail_mark_read(self, uid: int, a: Any) -> dict:
        return await self._modify_mail(uid, a, "Marked as read", [], ["UNREAD"])

    async def _mail_mark_unread(self, uid: int, a: Any) -> dict:
        return await self._modify_mail(uid, a, "Marked as unread", ["UNREAD"], [])

    async def _mail_label(self, uid: int, a: Any) -> dict:
        if a.lists_labels:
            labels = await self._labels(uid)
            return {"labels": [{"id": x.get("id", ""), "name": x.get("name", ""), "type": x.get("type", "")}
                               for x in labels]}
        if any(n.strip().upper() in SYSTEM_LABEL_LOCKED for n in a.add):
            raise GoogleError(FailureKind.INVALID_ARGUMENT,
                              "TRASH, DRAFT and SENT cannot be added as labels; use mail_trash to delete",
                              "add")
        add = await self._label_ids(uid, a.add, create=True)
        remove = await self._label_ids(uid, a.remove, create=False)
        return await self._modify_mail(uid, a, "Updated labels on", add, remove)

    async def _mail_trash(self, uid: int, a: Any) -> dict:
        return await self._trash(uid, a, "trash", "Moved to Trash")

    async def _mail_untrash(self, uid: int, a: Any) -> dict:
        return await self._trash(uid, a, "untrash", "Restored from Trash")

    async def _trash(self, uid: int, a: Any, step: str, verb: str) -> dict:
        await self._each(a.message_ids, lambda m: self._json(
            uid, "POST", f"{GMAIL}/messages/{_q(m)}/{step}", idempotent=True))
        await self._each(a.thread_ids, lambda t: self._json(
            uid, "POST", f"{GMAIL}/threads/{_q(t)}/{step}", idempotent=True))
        return {"verb": verb, "messages": len(a.message_ids), "threads": len(a.thread_ids),
                "added": [], "removed": []}

    # --- Calendar ----------------------------------------------------------------------------------

    async def _events(self, uid: int, params: dict, limit: int, calendar_id: str = "primary") -> dict:
        items, token, page = await self._pages(
            uid, f"{CALENDAR}/calendars/{_q(calendar_id)}/events",
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
        return await self._events(uid, params, a.max_results, a.calendar_id)

    async def _calendar_find(self, uid: int, a: Any) -> dict:
        now = datetime.now(UTC)
        return await self._events(uid, {
            "q": a.query, "timeMin": _rfc3339(now - FIND_WINDOW_BACK),
            "timeMax": _rfc3339(now + FIND_WINDOW_AHEAD),
        }, 25, a.calendar_id)

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
        if a.location:
            body["location"] = a.location
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
        if a.location is not None:
            body["location"] = a.location
        if a.attendees is not None:
            existing = {
                str(g.get("email", "")).lower(): g
                for g in (await self._json(uid, "GET", url)).get("attendees") or [] if isinstance(g, dict)
            }
            body["attendees"] = [existing.get(e.lower(), {"email": e}) for e in a.attendees]
        params = {"sendUpdates": "all"} if a.attendees else None
        # setting the same fields twice is harmless, but a repeat would notify the attendees twice
        return await self._json(uid, "PATCH", url, params=params, body=body, idempotent=params is None)

    async def _calendar_calendars(self, uid: int, a: Any) -> dict:
        items, token, _ = await self._pages(
            uid, f"{CALENDAR}/users/me/calendarList",
            {"maxResults": 250, "minAccessRole": "freeBusyReader"}, "items", 250, "maxResults")
        out: dict[str, Any] = {"calendars": items}
        if token:
            out["nextPageToken"] = token
        return out

    async def _calendar_get(self, uid: int, a: Any) -> dict:
        return await self._json(
            uid, "GET", f"{CALENDAR}/calendars/{_q(a.calendar_id)}/events/{_q(a.event_id)}")

    async def _calendar_delete(self, uid: int, a: Any) -> dict:
        # Google's own default for a delete is to tell nobody; guests are told only when the user agreed.
        # Deleting what is already deleted changes nothing and notifies nobody twice, so a repeat is safe.
        await self._json(
            uid, "DELETE", f"{CALENDAR}/calendars/{_q(a.calendar_id)}/events/{_q(a.event_id)}",
            params={"sendUpdates": "all" if a.notify_guests else "none"}, idempotent=True)
        return {"id": a.event_id, "deleted": True, "guestsNotified": a.notify_guests}

    async def _calendar_respond(self, uid: int, a: Any) -> dict:
        url = f"{CALENDAR}/calendars/{_q(a.calendar_id)}/events/{_q(a.event_id)}"
        event = await self._json(uid, "GET", url)
        guests = [g for g in event.get("attendees") or [] if isinstance(g, dict)]
        me = next((g for g in guests if g.get("self")), None)
        if me is None:
            raise GoogleError(FailureKind.INVALID_ARGUMENT,
                              "the user is not a guest on this event, so there is no invite to answer",
                              "event_id")
        me["responseStatus"] = a.response
        if a.comment:
            me["comment"] = a.comment
        # The organiser is told (sendUpdates=all), so a repeat after an unknown outcome could tell them twice.
        done = await self._json(uid, "PATCH", url, params={"sendUpdates": "all"},
                                body={"attendees": guests}, idempotent=False)
        return {"id": done.get("id", a.event_id), "summary": done.get("summary", event.get("summary", "")),
                "responseStatus": a.response}

    # --- Drive, Docs, Sheets -----------------------------------------------------------------------

    @staticmethod
    def _drive_q(query: str, *, extra: str = "") -> tuple[str, bool]:
        """(q, ordered). The model's query is never spliced into q. It is parsed against a small allowlisted
        grammar (see _DriveParser) and rebuilt with every value escaped; text that is not in that grammar is
        searched as plain words (`fullText contains '<escaped>'`). The result is ANDed with trashed = false,
        so no input can widen the search to trashed files or add a clause of its own. Drive refuses orderBy
        together with fullText terms, hence `ordered`."""
        text = query.strip()
        parsed = _DriveParser.parse(text) if text else None
        if text and parsed is None:
            parsed = (f"fullText contains {_drive_quote(text)}", True)
        clauses = [f"({parsed[0]})"] if parsed else []
        clauses += [extra] if extra else []
        clauses.append("trashed = false")
        return " and ".join(clauses), not (parsed and parsed[1])

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

    async def _drive_export_file(self, uid: int, a: Any) -> dict:
        """A Google Doc, Sheet or deck exported to `mime_type`, as base64 bytes (for an attachment)."""
        meta = await self._meta(uid, a.file_id)
        mime = str(meta.get("mimeType") or "")
        if not mime.startswith(GOOGLE_APPS) or not a.mime_type:
            raise GoogleError(FailureKind.INVALID_ARGUMENT, "only Google Docs, Sheets and Slides export",
                              "file_id")
        _, data, cut = await self._raw(uid, "GET", f"{DRIVE}/files/{_q(a.file_id)}/export",
                                       params={"mimeType": a.mime_type}, cap=EXPORT_CAP_BYTES)
        if cut:
            raise GoogleError(FailureKind.INVALID_ARGUMENT, "the exported file is over 10 MB", "file_id")
        return {"file_id": a.file_id, "name": meta.get("name", ""), "mimeType": mime,
                "content_b64": base64.b64encode(data).decode("ascii")}

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
        sheet = _q(a.spreadsheet_id)
        got = await self._json(uid, "GET", f"{SHEETS}/{sheet}/values/{_q(target, allow_slash=True)}",
                               params={"valueRenderOption": "FORMATTED_VALUE"})
        got["values"] = (got.get("values") or [])[:MAX_SHEET_ROWS]
        return {"valueRanges": [got]}

    # --- People ------------------------------------------------------------------------------------

    async def _warm(self, uid: int, kind: str, url: str, params: dict) -> None:
        """Google: the first search call of each kind must be an empty-query warm-up."""
        if (uid, kind) not in self._warmed:
            await self._json(uid, "GET", url, params={"query": "", **params})
            self._warmed.add((uid, kind))

    async def _contacts_search(self, uid: int, a: Any) -> dict:
        """Saved contacts first (errors here are real), then people the user has emailed (other contacts)
        and the Workspace directory. The last two are best effort: a grant without their scope, a personal
        account with no directory, or a Google error there is not a failure, it just adds nothing, and the
        reply names what was not searched."""
        saved_url = f"{PEOPLE}/people:searchContacts"
        await self._warm(uid, "contacts", saved_url, {"readMask": "names"})
        got = await self._json(uid, "GET", saved_url, params={
            "query": a.query, "pageSize": a.max_results, "readMask": PERSON_FIELDS})
        results = [{**r, "source": "contacts"} for r in got.get("results") or [] if isinstance(r, dict)]
        skipped: list[str] = []

        async def other() -> list[dict]:
            url = f"{PEOPLE}/otherContacts:search"
            await self._warm(uid, "other", url, {"readMask": "names"})
            page = await self._json(uid, "GET", url, params={
                "query": a.query, "pageSize": min(a.max_results, 30), "readMask": PERSON_FIELDS})
            return [{**r, "source": "other"} for r in page.get("results") or [] if isinstance(r, dict)]

        async def directory() -> list[dict]:
            page = await self._json(uid, "GET", f"{PEOPLE}/people:searchDirectoryPeople", params={
                "query": a.query, "pageSize": min(a.max_results, 500), "readMask": PERSON_FIELDS,
                "sources": ["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE", "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"]})
            return [{"person": p, "source": "directory"} for p in page.get("people") or []
                    if isinstance(p, dict)]

        for name, fetch in (("other contacts", other), ("company directory", directory)):
            try:
                results += await fetch()
            except GoogleError:
                skipped.append(name)
        seen: set[str] = set()
        unique = []
        for r in results:  # the same person can be in several sources: keep the first, richest, hit
            person = r.get("person") if isinstance(r.get("person"), dict) else {}
            emails = [str(e.get("value", "")).lower() for e in person.get("emailAddresses") or []
                      if isinstance(e, dict) and e.get("value")]
            key = emails[0] if emails else str(person.get("resourceName") or id(r))
            if key not in seen:
                seen.add(key)
                unique.append(r)
        out: dict[str, Any] = {"results": unique[: a.max_results * 2]}
        if skipped:
            out["not_searched"] = skipped
        return out

    @staticmethod
    def _person_body(a: Any) -> dict[str, Any]:
        """People API fields from our arguments (only the ones that were given)."""
        body: dict[str, Any] = {}
        name = getattr(a, "name", None)
        if name:
            given, _, family = name.strip().rpartition(" ")
            body["names"] = [{"givenName": given, "familyName": family} if given else {"givenName": family}]
        if getattr(a, "emails", None) is not None:
            body["emailAddresses"] = [{"value": e} for e in a.emails]
        if getattr(a, "phones", None) is not None:
            body["phoneNumbers"] = [{"value": p} for p in a.phones]
        company, title = getattr(a, "organization", None), getattr(a, "job_title", None)
        if company or title:
            body["organizations"] = [{k: v for k, v in (("name", company), ("title", title)) if v}]
        elif company == "" or title == "":
            body["organizations"] = []
        return body

    async def _contacts_create(self, uid: int, a: Any) -> dict:
        made = await self._json(uid, "POST", f"{PEOPLE}/people:createContact", idempotent=False,
                                params={"personFields": PERSON_FIELDS}, body=self._person_body(a))
        return {"resourceName": made.get("resourceName", ""), "id": made.get("resourceName", ""),
                "person": made}

    async def _contacts_update(self, uid: int, a: Any) -> dict:
        url = f"{PEOPLE}/{a.resource_name}"
        current = await self._json(uid, "GET", url, params={"personFields": "metadata," + PERSON_FIELDS})
        body = self._person_body(a)
        fields = {"names": "names", "emailAddresses": "emailAddresses", "phoneNumbers": "phoneNumbers",
                  "organizations": "organizations"}
        mask = ",".join(fields[k] for k in body)
        # the etag from the read makes Google refuse the write if the contact changed in between
        body["etag"] = current.get("etag", "")
        done = await self._json(uid, "PATCH", f"{url}:updateContact", idempotent=True, body=body,
                                params={"updatePersonFields": mask, "personFields": PERSON_FIELDS})
        return {"resourceName": done.get("resourceName", a.resource_name),
                "id": done.get("resourceName", a.resource_name), "person": done}

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

    # --- Drive, Docs and Sheets writes -----------------------------------------------------------------
    # idempotent=True only where sending the same request again leaves the same result (a PATCH or PUT to
    # a fixed value). A create, an append, a share, an upload or a comment makes one more thing each time,
    # so it is declared False and is never repeated once it may have reached Google.

    async def _drive_create_folder(self, uid: int, a: Any) -> dict:
        body: dict[str, Any] = {"name": a.name, "mimeType": FOLDER_MIME}
        if a.parent_id:
            body["parents"] = [a.parent_id]
        return await self._json(
            uid, "POST", f"{DRIVE}/files", idempotent=False, body=body,
            params={"supportsAllDrives": "true", "fields": WRITE_FILE_FIELDS})

    async def _drive_move(self, uid: int, a: Any) -> dict:
        remove = a.from_folder_id
        if not remove:  # a move leaves nothing behind: every current parent goes, except the destination
            current = await self._json(uid, "GET", f"{DRIVE}/files/{_q(a.file_id)}",
                                       params={"fields": "parents", "supportsAllDrives": "true"})
            remove = ",".join(p for p in current.get("parents") or [] if p != a.to_folder_id)
        params = {"addParents": a.to_folder_id, "supportsAllDrives": "true", "fields": WRITE_FILE_FIELDS}
        if remove and remove != a.to_folder_id:
            params["removeParents"] = remove
        return await self._json(uid, "PATCH", f"{DRIVE}/files/{_q(a.file_id)}", params=params, body={},
                                idempotent=True)

    async def _drive_share(self, uid: int, a: Any) -> dict:
        perm = await self._json(
            uid, "POST", f"{DRIVE}/files/{_q(a.file_id)}/permissions", idempotent=False,
            params={"sendNotificationEmail": "true", "supportsAllDrives": "true",
                    "fields": "id,type,role,emailAddress"},
            body={"type": "user", "role": a.role, "emailAddress": str(a.email)})
        return {"id": a.file_id, "permissionId": perm.get("id", ""), "role": perm.get("role", a.role),
                "emailAddress": perm.get("emailAddress", str(a.email))}

    async def _drive_upload_file(self, uid: int, a: Any) -> dict:
        content = await asyncio.to_thread(_read_artifact, a.path)
        mime = a.mime if _MIME.match(a.mime or "") else "application/octet-stream"
        meta: dict[str, Any] = {"name": a.name, "mimeType": mime}
        if a.folder_id:
            meta["parents"] = [a.folder_id]
        boundary = "mavis" + secrets.token_hex(16)
        body = b"".join((
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
            json.dumps(meta).encode(),
            f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode(), content,
            f"\r\n--{boundary}--".encode()))
        return await self._json(
            uid, "POST", DRIVE_UPLOAD, idempotent=False,
            raw=(body, f"multipart/related; boundary={boundary}"),
            params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": WRITE_FILE_FIELDS})

    async def _docs_create(self, uid: int, a: Any) -> dict:
        if len(a.markdown) > MAX_MARKDOWN_CHARS:
            raise GoogleError(FailureKind.INVALID_ARGUMENT,
                              f"document text is longer than {MAX_MARKDOWN_CHARS} characters", "markdown")
        requests = await asyncio.to_thread(markdown_requests, a.markdown)  # CPU work: not on the event loop
        doc = await self._json(uid, "POST", DOCS, idempotent=False, body={"title": a.title})
        doc_id = str(doc.get("documentId") or "")
        if not doc_id:
            raise GoogleError(FailureKind.UNCONFIRMED, UNCONFIRMED_DETAIL.format(
                what="google created a document but returned no id"))
        if requests:
            try:
                await self._batch_update(uid, doc_id, requests)
            except GoogleError as exc:
                if exc.kind is FailureKind.UNCONFIRMED:  # the text may be in: keep the doc, say which
                    raise GoogleError(
                        FailureKind.UNCONFIRMED, f"{exc.detail} (document id {doc_id})") from None
                await self._discard(uid, doc_id)
                raise
        return {"documentId": doc_id, "title": doc.get("title", a.title)}

    async def _discard(self, uid: int, file_id: str) -> None:
        """Best effort: trash a document whose body could not be written, so no empty one is left behind."""
        try:
            await self._json(uid, "PATCH", f"{DRIVE}/files/{_q(file_id)}", body={"trashed": True},
                             params={"supportsAllDrives": "true", "fields": "id"}, idempotent=True)
        except (GoogleError, ReauthRequired):
            pass

    async def _batch_update(self, uid: int, doc_id: str, requests: list[dict]) -> dict:
        return await self._json(uid, "POST", f"{DOCS}/{_q(doc_id)}:batchUpdate", idempotent=False,
                                body={"requests": requests})

    async def _docs_insert_text(self, uid: int, a: Any) -> dict:
        got = await self._batch_update(
            uid, a.document_id, [{"insertText": {"location": {"index": a.index}, "text": a.text}}])
        return {"documentId": got.get("documentId", a.document_id)}

    async def _docs_comment(self, uid: int, a: Any) -> dict:
        return await self._json(
            uid, "POST", f"{DRIVE}/files/{_q(a.file_id)}/comments", idempotent=False,
            params={"fields": "id,content,createdTime"}, body={"content": a.content})

    async def _sheets_create(self, uid: int, a: Any) -> dict:
        body: dict[str, Any] = {"properties": {"title": a.title}}
        if a.rows:  # created with its cells in the same call: one action, nothing left half done
            body["sheets"] = [{"data": [{"startRow": 0, "startColumn": 0, "rowData": [
                {"values": [_cell(v) for v in row]} for row in a.rows]}]}]
        made = await self._json(
            uid, "POST", SHEETS, idempotent=False, body=body,
            params={"fields": "spreadsheetId,spreadsheetUrl,properties.title"})
        return {"spreadsheetId": made.get("spreadsheetId", ""),
                "spreadsheetUrl": made.get("spreadsheetUrl", ""),
                "title": (made.get("properties") or {}).get("title", a.title)}

    async def _sheets_append_row(self, uid: int, a: Any) -> dict:
        target = a.range.strip() or "Sheet1"
        if "!" not in target and not target.startswith("'") and not _A1_ONLY.match(target):
            target = _sheet_ref(target)  # a bare sheet name that needs quoting (spaces, punctuation)
        return await self._json(
            uid, "POST", f"{SHEETS}/{_q(a.spreadsheet_id)}/values/{_q(target, allow_slash=True)}:append",
            idempotent=False,
            params={"valueInputOption": input_option(a.values), "insertDataOption": "INSERT_ROWS"},
            body={"majorDimension": "ROWS", "values": [list(a.values)]})

    async def _sheets_update_range(self, uid: int, a: Any) -> dict:
        target = f"{_sheet_ref(a.sheet_name)}!{a.start_cell.upper()}"
        rows = [list(r) for r in a.values]
        return await self._json(
            uid, "PUT", f"{SHEETS}/{_q(a.spreadsheet_id)}/values/{_q(target, allow_slash=True)}",
            idempotent=True, params={"valueInputOption": input_option(rows)},
            body={"majorDimension": "ROWS", "values": rows})

    # --- Tasks (the default list) ----------------------------------------------------------------------

    async def _tasks_list(self, uid: int, a: Any) -> dict:
        params: dict[str, Any] = {"maxResults": 100, "showCompleted": str(a.show_completed).lower(),
                                  "showHidden": str(a.show_completed).lower()}
        if a.due_before is not None:
            params["dueMax"] = _rfc3339(a.due_before)
        items, token, _ = await self._pages(uid, TASKS, params, "items", a.max_results, "maxResults")
        out: dict[str, Any] = {"tasks": items}  # the key workspace_render and attention.workspace read
        if token:
            out["nextPageToken"] = token
        return out

    async def _tasks_get(self, uid: int, a: Any) -> dict:
        return await self._json(uid, "GET", f"{TASKS}/{_q(a.task_id)}")

    async def _tasks_add(self, uid: int, a: Any) -> dict:
        body: dict[str, Any] = {"title": a.title, "status": "needsAction"}
        if a.notes:
            body["notes"] = a.notes
        if a.due is not None:
            body["due"] = _task_due(a.due)
        return await self._json(uid, "POST", TASKS, idempotent=False, body=body)

    async def _tasks_patch(self, uid: int, a: Any) -> dict:
        body: dict[str, Any] = {"title": a.title, "status": a.status}
        if a.status == "needsAction":
            body["completed"] = None  # reopening: drop the old completion time
        if a.notes is not None:
            body["notes"] = a.notes
        if a.due is not None:
            body["due"] = _task_due(a.due)
        return await self._json(uid, "PATCH", f"{TASKS}/{_q(a.task_id)}", body=body, idempotent=True)

    async def _tasks_delete(self, uid: int, a: Any) -> dict:
        await self._json(uid, "DELETE", f"{TASKS}/{_q(a.task_id)}", idempotent=True)
        return {"id": a.task_id, "deleted": True}

    # --- Slides and Forms ----------------------------------------------------------------------------

    async def _slides_read(self, uid: int, a: Any) -> dict:
        return await self._json(
            uid, "GET", f"{SLIDES}/{_q(a.presentation_id)}",
            params={"fields": "presentationId,title,slides(objectId,pageElements(shape(text),table))"})

    async def _slides_create(self, uid: int, a: Any) -> dict:
        slides = await asyncio.to_thread(parse_outline, a.outline)
        deck = await self._json(uid, "POST", SLIDES, idempotent=False, body={"title": a.title})
        deck_id = str(deck.get("presentationId") or "")
        if not deck_id:
            raise GoogleError(FailureKind.UNCONFIRMED, UNCONFIRMED_DETAIL.format(
                what="google created a presentation but returned no id"))
        if slides:
            requests = slide_requests(slides)
            # a new deck starts with one blank slide: it goes once the outline's slides are in
            requests += [{"deleteObject": {"objectId": str(sl["objectId"])}}
                         for sl in deck.get("slides") or [] if isinstance(sl, dict) and sl.get("objectId")]
            try:
                await self._json(uid, "POST", f"{SLIDES}/{_q(deck_id)}:batchUpdate", idempotent=False,
                                 body={"requests": requests})
            except GoogleError as exc:
                if exc.kind is FailureKind.UNCONFIRMED:
                    raise GoogleError(
                        FailureKind.UNCONFIRMED, f"{exc.detail} (presentation id {deck_id})") from None
                await self._discard(uid, deck_id)
                raise
        return {"presentationId": deck_id, "title": deck.get("title", a.title), "slides": len(slides),
                "url": f"https://docs.google.com/presentation/d/{deck_id}/edit"}

    async def _forms_read(self, uid: int, a: Any) -> dict:
        return await self._json(uid, "GET", f"{FORMS}/{_q(a.form_id)}")

    async def _forms_responses(self, uid: int, a: Any) -> dict:
        form = await self._json(uid, "GET", f"{FORMS}/{_q(a.form_id)}")
        responses: list[dict] = []
        token: str | None = None
        for _ in range(MAX_FORM_RESPONSES_PAGES):
            page = await self._json(uid, "GET", f"{FORMS}/{_q(a.form_id)}/responses", params={
                "pageSize": min(a.max_responses - len(responses), 500),
                **({"pageToken": token} if token else {})})
            responses += [r for r in page.get("responses") or [] if isinstance(r, dict)]
            token = page.get("nextPageToken")
            if len(responses) >= a.max_responses or not token:
                break
        return {"form": form, "responses": responses[: a.max_responses],
                "more": bool(token) or len(responses) > a.max_responses}

    # --- Meet --------------------------------------------------------------------------------------

    async def _meet_create(self, uid: int, a: Any) -> dict:
        return await self._json(uid, "POST", f"{MEET}/spaces", idempotent=False, body={})

    async def _participants(self, uid: int, record: str) -> dict[str, str]:
        """participant resource name -> display name, for one conference record."""
        got = await self._json(uid, "GET", f"{MEET}/conferenceRecords/{_q(record)}/participants",
                               params={"pageSize": MAX_MEET_PARTICIPANTS})
        out: dict[str, str] = {}
        for p in got.get("participants") or []:
            if not isinstance(p, dict):
                continue
            who = p.get("signedinUser") or p.get("anonymousUser") or p.get("phoneUser") or {}
            out[str(p.get("name", ""))] = str(who.get("displayName") or "Guest")
        return out

    async def _meet_recent(self, uid: int, a: Any) -> dict:
        since = datetime.now(UTC) - timedelta(days=a.days)
        try:
            records, _, _ = await self._pages(
                uid, f"{MEET}/conferenceRecords",
                {"filter": f'start_time>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"', "pageSize": 100},
                "conferenceRecords", 100, "pageSize")
        except GoogleError as exc:
            if exc.kind is not FailureKind.NOT_FOUND:  # no access to any record: nothing to list
                raise
            records = []
        records.sort(key=lambda r: str(r.get("startTime", "")), reverse=True)
        records = records[: a.max_results]
        gate = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def who(rec: dict) -> list[str]:
            async with gate:
                try:
                    people = await self._participants(uid, str(rec["name"]).split("/")[-1])
                    return sorted(set(people.values()))
                except GoogleError:
                    return []  # the list is useful without names

        names = await asyncio.gather(*(who(r) for r in records))
        return {"conferences": [
            {"conferenceRecordId": str(r.get("name", "")).split("/")[-1], "startTime": r.get("startTime", ""),
             "endTime": r.get("endTime", ""), "participants": n}
            for r, n in zip(records, names, strict=True)]}

    async def _meet_transcript(self, uid: int, a: Any) -> dict:
        """The transcripts of one conference and their spoken text. A call with no transcript (every call of
        a personal account, or one where transcription was off) is a normal answer, not an error."""
        record = _q(a.conference_record_id)
        try:
            items, _, _ = await self._pages(
                uid, f"{MEET}/conferenceRecords/{record}/transcripts", {"pageSize": 100},
                "transcripts", 100, "pageSize")
        except GoogleError as exc:
            if exc.kind is not FailureKind.NOT_FOUND:
                raise
            return {"transcripts": [], "available": False}
        try:
            speakers = await self._participants(uid, a.conference_record_id) if items else {}
        except GoogleError:
            speakers = {}
        out = []
        for t in items:
            entries: list[dict] = []
            if _TRANSCRIPT_NAME.match(str(t.get("name", ""))):
                try:
                    raw, _, _ = await self._pages(
                        uid, f"{MEET}/{t['name']}/entries", {"pageSize": 100},
                        "transcriptEntries", MAX_TRANSCRIPT_ENTRIES, "pageSize")
                except GoogleError as exc:
                    if exc.kind not in (FailureKind.NOT_FOUND, FailureKind.INVALID_ARGUMENT):
                        raise
                    raw = []
                entries = [{"speaker": speakers.get(str(e.get("participant", "")), "Participant"),
                            "text": e.get("text", ""), "startTime": e.get("startTime", "")} for e in raw]
            out.append({**t, "entries": entries})
        return {"transcripts": out, "available": bool(out)}


_TRANSCRIPT_NAME = re.compile(r"^conferenceRecords/[\w-]+/transcripts/[\w-]+$")
WRITE_FILE_FIELDS = "id,name,mimeType,parents,webViewLink"


def _sheet_ref(name: str) -> str:
    """A sheet name as an A1 prefix: always quoted, a quote inside doubled."""
    return "'" + name.replace("'", "''") + "'"


def _task_due(day: Any) -> str:
    return f"{day.isoformat()}T00:00:00.000Z"  # Tasks keeps the date only


def _read_artifact(raw: str) -> bytes:
    """The bytes of a file inside ARTIFACTS_DIR, at most UPLOAD_CAP_BYTES. Anything else is refused, so a
    path from a caller can never read outside the task artifacts (the caller has already checked that the
    file is this task's own; this is the second lock)."""
    path = Path(raw).resolve()
    if not path.is_relative_to(get_settings().artifacts_dir.resolve()):
        raise GoogleError(FailureKind.INVALID_ARGUMENT, "the file is not a task artifact", "path")
    try:
        with path.open("rb") as fh:
            data = fh.read(UPLOAD_CAP_BYTES + 1)
    except OSError:
        raise GoogleError(FailureKind.INVALID_ARGUMENT, "the file is missing", "path") from None
    if len(data) > UPLOAD_CAP_BYTES:
        raise GoogleError(FailureKind.INVALID_ARGUMENT, "the file is larger than 5 MB", "path")
    return data
