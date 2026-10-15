# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Outlook mail over Microsoft Graph: Gmail-syntax query translation, message mapping, mail actions.

Results use the key names GoogleExecutor emits (messageId, threadId, sender, subject, messageText, labelIds,
payload.headers with Authentication-Results first, ...) so normalize.py, mail_render and triage need no
Outlook-specific code. Ids carry an `ms:` prefix. Everything Graph returns is third-party data: bodies are
reduced to text (hidden content dropped), capped, and attachments are listed by name, type and size only.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from mavis.domain import timeutil
from mavis.domain.errors import FailureKind
from mavis.tools.integrations.actions import MAX_MAIL_IDS
from mavis.tools.integrations.native import gmail_mime
from mavis.tools.integrations.native.graph import GRAPH, GraphError, seg, strip_ms, with_ms
from mavis.tools.integrations.normalize import to_datetime

BODY_CAP = gmail_mime.BODY_CAP
SEARCH_BODY_CAP = 2000  # body chars kept per message in a list (the full text is mail.read)
THREAD_BODY_CAP = 8000
FETCH_CONCURRENCY = 5
MAX_THREAD_MESSAGES = 200
FOLDER_TTL_S = 600.0
ANCHOR = "receivedDateTime ge 1900-01-01T00:00:00Z"  # $orderby properties must lead the $filter
SELECT = (
    "id,conversationId,subject,bodyPreview,body,from,sender,toRecipients,ccRecipients,receivedDateTime,"
    "sentDateTime,isRead,isDraft,importance,inferenceClassification,categories,flag,hasAttachments,"
    "parentFolderId,internetMessageId,internetMessageHeaders"
)
# GraphClient already sends `Prefer: IdType="ImmutableId"`. `outlook.body-content-type="text"` is NOT sent:
# Exchange's own HTML to text conversion keeps text hidden with CSS, so the body is requested as sent and
# reduced by html_to_text, which drops hidden content.
WELL_KNOWN = {"inbox": "INBOX", "sentitems": "SENT", "drafts": "DRAFT", "deleteditems": "TRASH",
              "junkemail": "SPAM", "archive": "ARCHIVE"}
_RESERVED_LABELS = frozenset({"INBOX", "SENT", "DRAFT", "TRASH", "SPAM"})
_KEPT_HEADERS = ("list-unsubscribe", "precedence", "auto-submitted")

# --- query translation ---------------------------------------------------------------------------------

_TOKEN = re.compile(r'''[-+]?[({]*\w+:"[^"]*"|"[^"]*"|\S+''')
_OPERATOR = re.compile(r"^(?P<op>[A-Za-z_]\w*):(?P<val>.*)$", re.S)
_RELATIVE = re.compile(r"^(\d{1,4})([dmy])$")
_DAY_UNITS = {"d": 1, "m": 30, "y": 365}
KQL_OPS = frozenset({"from", "to", "subject"})
FOLDERS = {"inbox": "inbox", "sent": "sentitems", "drafts": "drafts", "trash": "deleteditems",
           "spam": "junkemail", "anywhere": None}
_CLOSERS = {")": "(", "}": "{"}


@dataclass(frozen=True)
class Translation:
    search: str | None
    filters: tuple[str, ...]
    local: tuple[Callable[[dict], bool], ...]
    folder: str | None
    unapplied: tuple[str, ...]
    since: datetime | None
    anywhere: bool = False  # `in:anywhere`: junk and deleted items are searched too


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _received(raw: dict) -> datetime | None:
    return to_datetime(raw.get("receivedDateTime"))


def _check_balanced(query: str) -> None:
    stack: list[str] = []
    in_quote = False
    for ch in query:
        if ch == '"':
            in_quote = not in_quote
        elif in_quote:
            continue
        elif ch in "({":
            stack.append(ch)
        elif ch in _CLOSERS:
            if not stack or stack.pop() != _CLOSERS[ch]:
                break
    else:
        if not in_quote and not stack:
            return
    raise GraphError(FailureKind.INVALID_ARGUMENT, "search query has an unclosed quote or bracket", "query")


def _date_term(op: str, value: str, now: datetime) -> tuple[str, datetime] | None:
    """(`ge` | `lt`, boundary) for one date operator, or None when the value is not understood."""
    if op in ("newer_than", "older_than"):
        m = _RELATIVE.match(value)
        if not m:
            return None
        bound = now - timedelta(days=int(m[1]) * _DAY_UNITS[m[2]])
        return ("ge" if op == "newer_than" else "lt"), bound
    try:
        day = datetime.strptime(value.replace("-", "/"), "%Y/%m/%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return ("ge" if op == "after" else "lt"), day


def translate_query(query: str, *, now: datetime) -> Translation:
    """Gmail search syntax to a Graph request. `$search` and `$filter` never combine: free text, from:, to:
    and subject: go to `$search`; dates and flags go to `$filter` when there is no `$search`, else they
    become `local` predicates over the returned page. Anything not translated is listed in `unapplied`."""
    _check_balanced(query)
    tokens = [t for t in _TOKEN.findall(query) if t not in ("-", "+")]
    while tokens and tokens[-1].upper() == "OR":
        tokens.pop()
    parts: list[str] = []
    unapplied: list[str] = []
    dates: list[tuple[str, datetime]] = []
    flags: list[tuple[str, Callable[[dict], bool]]] = []
    order: list[tuple[str, int]] = []  # input order across dates and flags, for the local predicates
    folder: str | None = None
    anywhere = False

    for tok in tokens:
        neg = tok.startswith("-")
        core = tok.lstrip("-+").lstrip("({").rstrip(")}") if not neg else tok.lstrip("-+({").rstrip(")}")
        if "{" in tok or "}" in tok:
            unapplied.append(tok)
            continue
        m = _OPERATOR.match(core)
        if m is None:
            if neg:
                unapplied.append(tok)
            elif core.upper() == "OR":
                parts.append("OR")
            elif core.startswith('"') and core.endswith('"') and len(core) > 2:
                parts.append(core)
            else:
                text = core.replace('"', "")
                if text:
                    parts.append(text)
            continue
        op, val = m["op"].lower(), m["val"]
        if neg:
            unapplied.append(tok)
        elif op in KQL_OPS:
            quoted = val.startswith('"') and val.endswith('"') and len(val) > 2
            inner = val.strip('"').replace('"', "")
            if not inner:
                unapplied.append(tok)
            else:
                parts.append(f'{op}:"{inner}"' if quoted else f"{op}:{inner}")
        elif op in ("newer_than", "older_than", "after", "before"):
            term = _date_term(op, val.strip('"'), now)
            if term is None:
                unapplied.append(tok)
            else:
                dates.append(term)
                order.append(("date", len(dates) - 1))
        elif op == "is" and val.lower() in ("unread", "read"):
            want_unread = val.lower() == "unread"
            flags.append((f"isRead eq {'false' if want_unread else 'true'}",
                          lambda r, u=want_unread: bool(r.get("isRead")) is not u))
            order.append(("flag", len(flags) - 1))
        elif op == "has" and val.lower() == "attachment":
            flags.append(("hasAttachments eq true", lambda r: r.get("hasAttachments") is True))
            order.append(("flag", len(flags) - 1))
        elif op == "in" and val.lower() in FOLDERS:
            folder = FOLDERS[val.lower()]
            anywhere = val.lower() == "anywhere"
        else:
            unapplied.append(tok)

    cleaned: list[str] = []
    for p in parts:
        if p == "OR" and (not cleaned or cleaned[-1] == "OR"):
            continue
        cleaned.append(p)
    while cleaned and cleaned[-1] == "OR":
        cleaned.pop()
    search = " ".join(cleaned) or None

    def predicate(kind: str, i: int) -> Callable[[dict], bool]:
        if kind == "flag":
            return flags[i][1]
        op, bound = dates[i]
        return (lambda r: (d := _received(r)) is not None and (d >= bound if op == "ge" else d < bound))

    since = max((b for op, b in dates if op == "ge"), default=None)
    if search is not None:
        return Translation(search, (), tuple(predicate(k, i) for k, i in order), folder,
                           tuple(unapplied), since, anywhere)
    filters = [f"receivedDateTime {op} {_stamp(b)}" for op, b in dates] or [ANCHOR]
    filters += [f for f, _ in flags]
    return Translation(None, tuple(filters), (), folder, tuple(unapplied), since, anywhere)


def kql_param(search: str) -> str:
    """The `$search` value: the whole expression in double quotes, inner phrase quotes backslash-escaped."""
    return '"' + " ".join(search.split()).replace('"', '\\"') + '"'


# --- body to text --------------------------------------------------------------------------------------

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                   "source", "track", "wbr"})
_SKIP = frozenset({"script", "style", "head", "title", "template", "noscript", "iframe", "object", "svg"})
_BLOCK = gmail_mime._BLOCK_TAGS
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all|opacity\s*:\s*0(?![.\d])"
    r"|font-size\s*:\s*0+(?:\.0+)?\s*(?:px|pt|em|rem|%)?\s*(?:[;!]|$)", re.I)


def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name == "hidden":
            return True
        if name == "style" and value and _HIDDEN_STYLE.search(value):
            return True
    return False


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._hidden = 0

    def _emit(self, text: str) -> None:
        if not self._hidden:
            self.parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if tag in ("br", "hr"):
                self._emit("\n")
            return
        hide = tag in _SKIP or _is_hidden(attrs)
        self._stack.append((tag, hide))
        if hide:
            self._hidden += 1
        elif tag in _BLOCK:
            self._emit("\n- " if tag == "li" else "\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr") or tag in _BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        names = [t for t, _ in self._stack]
        if tag not in names:
            return
        cut = len(names) - 1 - names[::-1].index(tag)
        for _, hid in self._stack[cut:]:
            if hid:
                self._hidden -= 1
        del self._stack[cut:]
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        self._emit(data)


def html_to_text(html: str) -> str:
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup degrades to whatever text was seen
        pass
    return gmail_mime.tidy("".join(parser.parts))


# --- message mapping -----------------------------------------------------------------------------------

_NEEDS_QUOTES = re.compile(r'[,;:<>@()\[\]\\".]')


def _address(who: Any) -> str:
    box = who.get("emailAddress") if isinstance(who, dict) else None
    if not isinstance(box, dict):
        return ""
    addr, name = str(box.get("address") or "").strip(), str(box.get("name") or "").strip()
    if not addr:
        return name
    if not name or name.lower() == addr.lower():
        return addr
    if _NEEDS_QUOTES.search(name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def _addresses(items: Any) -> str:
    return ", ".join(a for a in (_address(x) for x in items or []) if a)


def _labels(raw: dict, folders: Mapping[str, str]) -> list[str]:
    folder = folders.get(str(raw.get("parentFolderId") or ""))
    labels: list[str] = []
    if folder in ("INBOX", "SENT", "DRAFT", "TRASH", "SPAM"):
        labels.append(folder)
    if raw.get("isDraft") and "DRAFT" not in labels:
        labels.append("DRAFT")
    if raw.get("isRead") is False:
        labels.append("UNREAD")
    if raw.get("importance") == "high":
        labels.append("IMPORTANT")
    if (raw.get("flag") or {}).get("flagStatus") == "flagged":
        labels.append("STARRED")
    if folder == "INBOX":
        cls = raw.get("inferenceClassification")
        labels.append("CATEGORY_OTHER" if cls == "other" else "CATEGORY_PRIMARY")
    labels += [str(c) for c in raw.get("categories") or [] if c]
    return labels


def _headers(raw: dict) -> list[dict[str, str]]:
    held = [h for h in raw.get("internetMessageHeaders") or [] if isinstance(h, dict) and h.get("name")]
    value = lambda h: str(h.get("value", ""))[:1000]  # noqa: E731
    out: list[dict[str, str]] = []
    first = next((h for h in held if str(h["name"]).lower() == "authentication-results"), None)
    if first is not None:  # the topmost one is the receiving server's own
        out.append({"name": "Authentication-Results", "value": value(first)})
    out += [{"name": "From", "value": _address(raw.get("from") or raw.get("sender"))},
            {"name": "To", "value": _addresses(raw.get("toRecipients"))},
            {"name": "Subject", "value": str(raw.get("subject") or "")}]
    for key in _KEPT_HEADERS:
        h = next((h for h in held if str(h["name"]).lower() == key), None)
        if h is not None:
            out.append({"name": str(h["name"]), "value": value(h)})
    return out


def _body(raw: dict, cap: int) -> tuple[str, bool]:
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    content = str(body.get("content") or "")
    is_html = str(body.get("contentType", "")).lower() == "html"
    text = html_to_text(content) if is_html else gmail_mime.tidy(content)
    return (text[:cap].rstrip(), True) if len(text) > cap else (text, False)


def map_message(raw: dict, folders: Mapping[str, str], cap: int) -> dict[str, Any]:
    text, truncated = _body(raw, cap)
    snippet = str(raw.get("bodyPreview") or "")
    when = raw.get("receivedDateTime") or raw.get("sentDateTime")
    return {
        "messageId": with_ms(str(raw.get("id") or "")),
        "threadId": with_ms(str(raw.get("conversationId") or "")),
        "sender": _address(raw.get("from") or raw.get("sender")),
        "to": _addresses(raw.get("toRecipients")),
        "cc": _addresses(raw.get("ccRecipients")),
        "subject": str(raw.get("subject") or ""),
        "date": when, "rfc822MessageId": str(raw.get("internetMessageId") or ""),
        "messageText": text or snippet, "bodyTruncated": truncated, "snippet": snippet,
        "labelIds": _labels(raw, folders), "messageTimestamp": when,
        "attachments": [
            {"filename": str(a.get("name") or "(unnamed)"), "mimeType": str(a.get("contentType") or ""),
             "size": int(a.get("size") or 0)}
            for a in raw.get("attachments") or [] if isinstance(a, dict)
        ],
        "payload": {"headers": _headers(raw)},
    }


# --- actions -------------------------------------------------------------------------------------------


def _recipients(addresses: list[str]) -> list[dict]:
    return [{"emailAddress": {"address": a}} for a in addresses]


def _one_line(text: str) -> str:
    return " ".join(text.split())


class MailActions:
    """The mail.* handlers. `ex` is the MicrosoftExecutor (only `ex.graph` is used)."""

    def __init__(self, ex: Any) -> None:
        self.graph = ex.graph
        self._folders: dict[int, tuple[float, dict[str, str]]] = {}

    async def _call(self, uid: int, method: str, url: str, **kw: Any) -> Any:
        return await self.graph.json(uid, method, url, **kw)

    async def folder_ids(self, uid: int) -> dict[str, str]:
        """Graph folder id to INBOX/SENT/DRAFT/TRASH/SPAM/ARCHIVE, cached ten minutes per user."""
        hit = self._folders.get(uid)
        if hit and hit[0] > time.monotonic():
            return hit[1]

        async def one(name: str) -> tuple[str, str] | None:
            try:
                got = await self._call(uid, "GET", f"{GRAPH}/me/mailFolders/{name}", params={"$select": "id"})
            except GraphError as exc:
                if exc.kind is FailureKind.NOT_FOUND:  # e.g. no Archive folder yet
                    return None
                raise
            fid = got.get("id") if isinstance(got, dict) else None
            return (str(fid), WELL_KNOWN[name]) if fid else None

        found = await asyncio.gather(*(one(n) for n in WELL_KNOWN))
        mapping = dict(x for x in found if x)
        self._folders[uid] = (time.monotonic() + FOLDER_TTL_S, mapping)
        return mapping

    async def search(self, uid: int, a: Any) -> dict:
        tr = translate_query(a.query, now=timeutil.now())
        url = f"{GRAPH}/me/mailFolders/{tr.folder}/messages" if tr.folder else f"{GRAPH}/me/messages"
        top = min(a.max_results * 2 if (tr.local or not tr.folder) else a.max_results, 50)
        params: dict[str, Any] = {"$select": SELECT, "$top": top}
        if tr.search is not None:
            params["$search"] = kql_param(tr.search)
        else:
            params["$filter"] = " and ".join(tr.filters)
            params["$orderby"] = "receivedDateTime desc"
        items, _, _ = await self._pages(uid, url, params, top)
        folders = await self.folder_ids(uid)
        notes: list[str] = []
        if tr.unapplied:
            notes.append("part of the query was not applied: " + ", ".join(tr.unapplied))
        if tr.local and len(items) >= top:
            notes.append(f"date and flag filters were applied to the first {len(items)} results only")
        if not tr.folder and not tr.anywhere:
            hidden = ("TRASH", "SPAM")
            items = [m for m in items if folders.get(str(m.get("parentFolderId") or "")) not in hidden]
        for keep in tr.local:
            items = [m for m in items if keep(m)]
        items.sort(key=lambda m: _received(m) or datetime.min.replace(tzinfo=UTC), reverse=True)
        items = items[: a.max_results]
        out: dict[str, Any] = {"messages": [map_message(m, folders, SEARCH_BODY_CAP) for m in items],
                               "resultSizeEstimate": len(items)}
        if notes:
            out["notes"] = notes
        return out

    async def _pages(self, uid: int, url: str, params: dict, limit: int) -> tuple[list[dict], Any, Any]:
        return await self.graph.pages(uid, url, params, limit)

    async def read(self, uid: int, a: Any) -> dict:
        raw = await self._call(uid, "GET", f"{GRAPH}/me/messages/{seg(a.message_id)}", params={
            "$select": SELECT, "$expand": "attachments($select=name,contentType,size)"})
        return map_message(raw, await self.folder_ids(uid), BODY_CAP)

    async def _conversation(self, uid: int, thread_id: str, select: str, limit: int) -> list[dict]:
        conv = strip_ms(thread_id).replace("'", "''")
        items, _, _ = await self._pages(
            uid, f"{GRAPH}/me/messages", {"$filter": f"conversationId eq '{conv}'", "$select": select,
                                          "$top": 50}, limit)
        return items

    async def thread(self, uid: int, a: Any) -> dict:
        items = await self._conversation(uid, a.thread_id, SELECT, MAX_THREAD_MESSAGES)
        items.sort(key=lambda m: _received(m) or datetime.min.replace(tzinfo=UTC))
        folders = await self.folder_ids(uid)
        return {"threadId": with_ms(strip_ms(a.thread_id)),
                "messages": [map_message(m, folders, THREAD_BODY_CAP) for m in items]}

    async def draft(self, uid: int, a: Any) -> dict:
        made = await self._call(uid, "POST", f"{GRAPH}/me/messages", body=self._message(a))
        mid = with_ms(str(made.get("id") or ""))
        return {"draftId": mid, "id": mid, "messageId": mid,
                "threadId": with_ms(str(made.get("conversationId") or ""))}

    async def send(self, uid: int, a: Any) -> dict:
        await self._call(uid, "POST", f"{GRAPH}/me/sendMail",
                         body={"message": self._message(a), "saveToSentItems": True})
        return {"messageId": "", "id": "", "threadId": "", "labelIds": ["SENT"]}

    @staticmethod
    def _message(a: Any) -> dict:
        return {"subject": _one_line(a.subject), "body": {"contentType": "text", "content": a.body},
                "toRecipients": _recipients(list(a.to)), "ccRecipients": _recipients(list(a.cc))}

    async def reply(self, uid: int, a: Any) -> dict:
        items = [m for m in await self._conversation(
            uid, a.thread_id, "id,conversationId,receivedDateTime,sentDateTime,isDraft", MAX_THREAD_MESSAGES)
            if not m.get("isDraft")]
        if not items:
            raise GraphError(FailureKind.NOT_FOUND, "microsoft conversation has no messages")
        floor = datetime.min.replace(tzinfo=UTC)
        newest = max(items, key=lambda m: _received(m) or to_datetime(m.get("sentDateTime")) or floor)
        await self._call(uid, "POST", f"{GRAPH}/me/messages/{seg(str(newest['id']))}/reply", body={
            "message": {"toRecipients": _recipients([a.to])}, "comment": a.body})
        return {"messageId": "", "id": "", "threadId": with_ms(strip_ms(a.thread_id)), "labelIds": ["SENT"]}

    # --- organising ---

    async def _ids(self, uid: int, a: Any) -> list[str]:
        """message_ids plus every message of each thread_id, de-duplicated."""
        ids = [strip_ms(m) for m in a.message_ids]
        for t in a.thread_ids:
            ids += [str(m["id"]) for m in await self._conversation(uid, t, "id", MAX_MAIL_IDS)]
        ids = list(dict.fromkeys(ids))
        if len(ids) > MAX_THREAD_MESSAGES:
            raise GraphError(FailureKind.INVALID_ARGUMENT,
                             "those conversations hold too many messages; pass fewer thread_ids",
                             "thread_ids")
        return ids

    @staticmethod
    async def _each(items: list[str], call: Callable[[str], Awaitable[Any]]) -> None:
        gate = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(item: str) -> None:
            async with gate:
                await call(item)

        for r in await asyncio.gather(*(one(i) for i in items), return_exceptions=True):
            if isinstance(r, BaseException):
                raise r

    @staticmethod
    def _summary(a: Any, verb: str, added: list[str], removed: list[str]) -> dict:
        return {"verb": verb, "messages": len(a.message_ids), "threads": len(a.thread_ids),
                "added": added, "removed": removed}

    async def _move(self, uid: int, a: Any, dest: str, verb: str) -> dict:
        await self._each(await self._ids(uid, a), lambda m: self._call(
            uid, "POST", f"{GRAPH}/me/messages/{seg(m)}/move", idempotent=True, body={"destinationId": dest}))
        return self._summary(a, verb, [], [])

    async def _patch(self, uid: int, a: Any, body: dict, verb: str) -> dict:
        await self._each(await self._ids(uid, a), lambda m: self._call(
            uid, "PATCH", f"{GRAPH}/me/messages/{seg(m)}", idempotent=True, body=body))
        return self._summary(a, verb, [], [])

    async def archive(self, uid: int, a: Any) -> dict:
        return await self._move(uid, a, "archive", "Archived")

    async def trash(self, uid: int, a: Any) -> dict:
        return await self._move(uid, a, "deleteditems", "Moved to Trash")

    async def untrash(self, uid: int, a: Any) -> dict:
        return await self._move(uid, a, "inbox", "Restored from Trash")

    async def mark_read(self, uid: int, a: Any) -> dict:
        return await self._patch(uid, a, {"isRead": True}, "Marked as read")

    async def mark_unread(self, uid: int, a: Any) -> dict:
        return await self._patch(uid, a, {"isRead": False}, "Marked as unread")

    async def label(self, uid: int, a: Any) -> dict:
        if a.lists_labels:
            got = await self._call(uid, "GET", f"{GRAPH}/me/outlook/masterCategories")
            return {"labels": [{"id": x.get("id", ""), "name": x.get("displayName", ""), "type": "user"}
                               for x in got.get("value") or [] if isinstance(x, dict)]}
        for field, names in (("add", a.add), ("remove", a.remove)):
            if any(n.strip().upper() in _RESERVED_LABELS for n in names):
                raise GraphError(FailureKind.INVALID_ARGUMENT,
                                 "INBOX, SENT, DRAFT, TRASH and SPAM are folders; use mail_archive or "
                                 "mail_trash to move mail", field)

        def special(names: list[str], word: str) -> bool:
            return any(n.strip().upper() == word for n in names)

        async def one(mid: str) -> None:
            cats = (await self._call(uid, "GET", f"{GRAPH}/me/messages/{seg(mid)}",
                                     params={"$select": "id,categories"})).get("categories") or []
            drop = {n.strip().casefold() for n in a.remove}
            merged = [c for c in cats if str(c).casefold() not in drop]
            for n in a.add:
                n = n.strip()
                if n and n.upper() not in ("STARRED", "IMPORTANT", "UNREAD") and n.casefold() not in {
                        str(c).casefold() for c in merged}:
                    merged.append(n)
            patch: dict[str, Any] = {}
            if merged != list(cats):
                patch["categories"] = merged
            for word, key, on, off in (
                    ("STARRED", "flag", {"flagStatus": "flagged"}, {"flagStatus": "notFlagged"}),
                    ("IMPORTANT", "importance", "high", "normal"), ("UNREAD", "isRead", False, True)):
                if special(a.add, word):
                    patch[key] = on
                elif special(a.remove, word):
                    patch[key] = off
            if patch:
                await self._call(uid, "PATCH", f"{GRAPH}/me/messages/{seg(mid)}", idempotent=True, body=patch)

        await self._each(await self._ids(uid, a), one)
        return self._summary(a, "Updated labels on", list(a.add), list(a.remove))


def mail_handlers(ex: Any) -> dict[str, Callable[[int, Any], Awaitable[Any]]]:
    m = MailActions(ex)
    return {"mail.search": m.search, "mail.read": m.read, "mail.thread": m.thread, "mail.draft": m.draft,
            "mail.send": m.send, "mail.reply": m.reply, "mail.archive": m.archive,
            "mail.mark_read": m.mark_read, "mail.mark_unread": m.mark_unread, "mail.label": m.label,
            "mail.trash": m.trash, "mail.untrash": m.untrash}
