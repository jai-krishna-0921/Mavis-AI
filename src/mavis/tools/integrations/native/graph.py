# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Microsoft Graph transport: one authorised call with the retry rules, size caps and id validation.

The rules mirror GoogleExecutor._raw. Graph answers are third-party data; an error body is reduced to
`microsoft <status> <error.code>` and its `message` (which can echo mailboxes and ids) is never copied.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from mavis.domain.errors import FailureKind
from mavis.tools.integrations.native.base import NativeProvider, TokenSource
from mavis.tools.integrations.native.google import NEVER_SENT, SAFE_METHODS, _retry_after

GRAPH_HOST = "graph.microsoft.com"
GRAPH = f"https://{GRAPH_HOST}/v1.0"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_RETRY_WAIT_S = 10.0
DEFAULT_RETRY_WAIT_S = 1.0
SERVER_BACKOFF_S = 1.0
MAX_PAGES = 10
MS_ID_PREFIX = "ms:"
REDIRECTS = (301, 302, 303, 307, 308)
# Graph ids: base64url (- _ =), Teams chat ids (: @ .), no / ? # % \
_ID_OK = re.compile(r"[A-Za-z0-9_\-=.:@!$+~]+")
_CODE_OK = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_ACCESS_SCOPED = ("/chats", "/drive", "/drives")  # a 403 here is a missing permission, not "absent"
_TEAMS_PATHS = ("/chats", "/teams")
_MAIL_PATHS = ("/me/messages", "/me/mailFolders", "/me/sendMail")
# Answers of a licence-less account (no Exchange Online mailbox): never "not found".
_NO_MAILBOX_CODES = frozenset({"MailboxNotEnabledForRESTAPI", "MailboxNotSupportedForRESTAPI"})
NO_MAILBOX = "this account has no Outlook mailbox (or mail access is not allowed for it)"
_HINT_MEMBER, _HINT_SCOPE = "member", "scope"
_MEMBER_WORDS = ("not a member", "not a participant")
_SCOPE_WORDS = ("missing scope", "missing role", "insufficient privileges", "admin consent", "aadsts65001")
_SCOPE_DETAIL = "permission or admin consent missing"
_MEMBER_DETAIL = "not a member of that chat"
UNCONFIRMED_DETAIL = (
    "microsoft outcome unknown: {what}. The request may or may not have been processed, so do NOT repeat it "
    "blindly. Tell the user it may have gone through and to check (for mail, the Sent folder; for a "
    "calendar event, the calendar) before trying again."
)


class GraphError(Exception):
    def __init__(self, kind: FailureKind, detail: str, field: str | None = None) -> None:
        super().__init__(detail)
        self.kind, self.detail, self.field = kind, detail, field


def strip_ms(value: str) -> str:
    return value[len(MS_ID_PREFIX):] if value.startswith(MS_ID_PREFIX) else value


def with_ms(value: str) -> str:
    return value if value.startswith(MS_ID_PREFIX) else MS_ID_PREFIX + value


def bad_segment(value: str) -> bool:
    """Would this value, used as one URL path segment, address something else? Dot segments, anything outside
    the characters real Graph ids use (so no slash, backslash, query, fragment, percent or control char)."""
    v = strip_ms(value)
    return (v in ("", ".", "..") or v.startswith("$") or v != v.strip()
            or not _ID_OK.fullmatch(v))


def seg(value: str) -> str:
    if bad_segment(value):
        raise GraphError(FailureKind.INVALID_ARGUMENT, "invalid id")
    return quote(strip_ms(value), safe=":@=!")


def _bad_graph_url(url: str) -> bool:
    """Is this anything but a plain https URL on the Graph host (default port, no userinfo)? A link that
    does not even parse counts as bad."""
    try:
        target = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return True
    return (target.scheme != "https" or target.host != GRAPH_HOST or target.port not in (None, 443)
            or bool(target.userinfo))


def _prefer(extra: str | None) -> str:
    """Prefer values are comma-joined; IdType="ImmutableId" is always kept so stored ids stay stable."""
    values = [v.strip() for v in (extra or "").split(",") if v.strip()]
    if not any(v.lower().startswith("idtype") for v in values):
        values.insert(0, 'IdType="ImmutableId"')
    return ", ".join(values)


def _error_code(data: bytes) -> str:
    try:
        code = json.loads(data)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""
    return code if isinstance(code, str) and _CODE_OK.fullmatch(code) else ""


def _error_hint(data: bytes) -> str:
    """What a 403 means, read from fixed phrases in error.message. The message itself is never kept: only
    the verdict ("member" or "scope", else "") leaves this function."""
    try:
        err = json.loads(data)["error"]
        message = err.get("message")
        code = err.get("code")
    except (ValueError, KeyError, TypeError, AttributeError):
        return ""
    text = message[:2000].lower() if isinstance(message, str) else ""
    if any(w in text for w in _MEMBER_WORDS):
        return _HINT_MEMBER
    if code == "Authorization_RequestDenied" or any(w in text for w in _SCOPE_WORDS):
        return _HINT_SCOPE
    return ""


def classify(status: int, code: str, detail: str, url: str = "",
             on_forbidden: FailureKind | None = None, hint: str = "") -> GraphError:
    """Map a Graph status and error.code to a FailureKind. `detail` is passed through unchanged.
    `on_forbidden` is the kind a 403 gets when the caller knows what it means (else the URL decides).
    `hint` (see `_error_hint`) separates, on Teams endpoints, a missing permission or admin consent from a
    chat the user is not in."""
    path = httpx.URL(url).path.removeprefix("/v1.0") if url else ""
    denied_send = status == 403 and code == "ErrorAccessDenied" and path == "/me/sendMail"
    if path.startswith(_MAIL_PATHS) and (code in _NO_MAILBOX_CODES or denied_send):
        return GraphError(FailureKind.UNAVAILABLE, f"{detail}: {NO_MAILBOX}")
    if status == 403 and hint and any(p in path for p in _TEAMS_PATHS):
        if hint == _HINT_MEMBER:
            return GraphError(FailureKind.NOT_FOUND, f"{detail}: {_MEMBER_DETAIL}")
        return GraphError(FailureKind.PERMISSION_MISSING, f"{detail}: {_SCOPE_DETAIL}")
    if status == 400:
        kind = FailureKind.NOT_FOUND if code == "ErrorInvalidIdMalformed" else FailureKind.INVALID_ARGUMENT
        return GraphError(kind, detail)
    if status == 401:
        return GraphError(FailureKind.AUTH, detail)
    if status == 403:
        if on_forbidden is not None:
            return GraphError(on_forbidden, detail)
        if any(part in path for part in _ACCESS_SCOPED):
            return GraphError(FailureKind.PERMISSION_MISSING, detail)
        return GraphError(FailureKind.NOT_FOUND, detail)  # no access to that item: as good as absent
    if status in (404, 410):
        return GraphError(FailureKind.NOT_FOUND, detail)
    if status in (409, 412, 413, 422):
        return GraphError(FailureKind.INVALID_ARGUMENT, detail)
    if status == 423:
        return GraphError(FailureKind.UNAVAILABLE, detail)
    return GraphError(FailureKind.UNKNOWN, detail)


class GraphClient:
    def __init__(self, tokens: TokenSource, client: httpx.AsyncClient, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._tokens, self._client, self._sleep = tokens, client, sleep
        self.max_json = MAX_JSON_BYTES

    async def _send(self, token: str, method: str, url: str, params: dict | None, body: Any,
                    headers: dict | None, cap: int) -> tuple[int, httpx.Headers, bytes, bool]:
        extra = {k: v for k, v in (headers or {}).items()}
        prefer = ", ".join(v for k, v in extra.items() if k.lower() == "prefer")
        hdrs = {k: v for k, v in extra.items() if k.lower() not in ("authorization", "host", "prefer")}
        hdrs["Authorization"] = f"Bearer {token}"
        hdrs["Prefer"] = _prefer(prefer)
        kw: dict[str, Any] = {"headers": hdrs, "params": params}
        if body is not None:
            kw["json"] = body
        async with self._client.stream(method, url, **kw) as resp:
            chunks, size, cut = [], 0, False
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > cap:
                    cut = True
                    break
            return resp.status_code, resp.headers, b"".join(chunks)[:cap], cut

    def fresh_client(self) -> httpx.AsyncClient:
        """A client on the same transport with its own empty cookie jar, no default credentials and no
        redirects: for the pre-authenticated download, so nothing is shared with Graph calls. Its transport
        belongs to the shared client, so it is deliberately not closed."""
        base = self._client
        return httpx.AsyncClient(
            transport=base._transport,  # noqa: SLF001
            timeout=base.timeout, follow_redirects=False, trust_env=False,
            headers={"User-Agent": base.headers.get("user-agent", "mavis-ai/1.0"),
                     "Accept-Encoding": "identity"},
        )

    @property
    def http(self) -> httpx.AsyncClient:
        """The shared HTTP client, for the one caller that fetches a pre-authenticated download URL."""
        return self._client

    async def raw(self, uid: int, method: str, url: str, **kw: Any) -> tuple[httpx.Headers, bytes, bool]:
        _, headers, data, cut = await self.raw_status(uid, method, url, **kw)
        return headers, data, cut

    async def raw_status(
        self, uid: int, method: str, url: str, *, params: dict | None = None, body: Any = None,
        headers: dict | None = None, cap: int | None = None, idempotent: bool | None = None,
        on_forbidden: FailureKind | None = None, accept_redirect: bool = False,
    ) -> tuple[int, httpx.Headers, bytes, bool]:
        """One authorised call with the retry rules (see GoogleExecutor._raw). A 429 or a 503 with Retry-After
        was rejected before it ran, so any call may repeat after waiting once. A 504 or other 5xx or a read
        error may come after Graph acted: only an idempotent call repeats, otherwise UNCONFIRMED."""
        if _bad_graph_url(url):
            raise GraphError(FailureKind.INVALID_ARGUMENT, "refusing a non-Graph url")
        if idempotent is None:
            idempotent = method.upper() in SAFE_METHODS
        cap = self.max_json if cap is None else cap
        token = await self._tokens.access_token(uid, NativeProvider.MICROSOFT)
        refreshed = throttled = backed_off = maybe_ran = False
        write = method.upper() not in SAFE_METHODS

        def give_up(kind: FailureKind, why: str) -> GraphError:
            # maybe_ran: a post-send transport error, a 5xx or a 504 came back, so an earlier attempt of this
            # write may have been carried out. A request that never left (connect errors) did not run.
            if write and maybe_ran:
                return GraphError(FailureKind.UNCONFIRMED, UNCONFIRMED_DETAIL.format(what=why))
            return GraphError(kind, why)

        while True:
            try:
                status, resp_headers, data, cut = await self._send(
                    token, method, url, params, body, headers, cap)
            except httpx.TransportError as exc:
                maybe_ran = maybe_ran or not isinstance(exc, NEVER_SENT)
                if not (isinstance(exc, NEVER_SENT) or idempotent):
                    raise GraphError(FailureKind.UNCONFIRMED, UNCONFIRMED_DETAIL.format(
                        what=f"the connection failed after the request was sent ({type(exc).__name__})")
                    ) from None
                if backed_off:
                    raise give_up(FailureKind.UNAVAILABLE,
                                  f"microsoft unreachable ({type(exc).__name__})") from None
                backed_off = True
                await self._sleep(SERVER_BACKOFF_S)
                continue
            if status < 300 or (accept_redirect and status in REDIRECTS):
                return status, resp_headers, data, cut  # a redirect is handed back for the caller to judge
            code = _error_code(data)
            detail = f"microsoft {status} {code}".strip()
            if status == 401:
                if refreshed:
                    raise GraphError(FailureKind.AUTH, detail)
                token = await self._tokens.access_token(uid, NativeProvider.MICROSOFT, force=True)
                refreshed = True
                continue
            retry_after = _retry_after(resp_headers)
            maybe_ran = maybe_ran or status >= 500 and status != 503 or status == 503 and retry_after is None
            if status == 429 or (status == 503 and retry_after is not None) or (
                    status == 504 and retry_after is not None and idempotent):
                wait = DEFAULT_RETRY_WAIT_S if retry_after is None else retry_after
                if throttled or wait > MAX_RETRY_WAIT_S:
                    raise give_up(FailureKind.RATE_LIMITED if status != 504 else FailureKind.UNAVAILABLE,
                                  detail)
                throttled = True
                await self._sleep(wait)
                continue
            if status >= 500:
                if not idempotent:
                    raise GraphError(FailureKind.UNCONFIRMED, UNCONFIRMED_DETAIL.format(what=detail))
                if backed_off:
                    raise give_up(FailureKind.UNAVAILABLE, detail)
                backed_off = True
                await self._sleep(SERVER_BACKOFF_S)
                continue
            hint = _error_hint(data) if status == 403 else ""
            raise classify(status, code, detail, url, on_forbidden, hint)

    async def json(self, uid: int, method: str, url: str, **kw: Any) -> Any:
        _, data, cut = await self.raw(uid, method, url, **kw)
        if cut:
            raise GraphError(FailureKind.UNKNOWN, "microsoft response larger than the size limit")
        if not data.strip():
            return {}
        try:
            return json.loads(data)
        except ValueError:
            raise GraphError(FailureKind.UNKNOWN, "microsoft returned a body that is not JSON") from None

    async def pages(
        self, uid: int, url: str, params: dict | None, limit: int, *, max_pages: int = MAX_PAGES,
        headers: dict | None = None, on_forbidden: FailureKind | None = None,
    ) -> tuple[list[dict], str | None, str | None]:
        """Follow @odata.nextLink (verbatim, params only on the first request) until `limit` items.
        `headers` (e.g. a Prefer timezone) are sent on every page.
        Returns (items, nextLink if more, deltaLink if present)."""
        items: list[dict] = []
        next_link: str | None = None
        delta_link: str | None = None
        target, query = url, params
        total = 0
        for _ in range(max_pages):
            _, raw, cut = await self.raw(uid, "GET", target, params=query, headers=headers,
                                          on_forbidden=on_forbidden)
            total += len(raw)
            if cut:
                raise GraphError(FailureKind.UNKNOWN, "microsoft response larger than the size limit")
            try:
                data = json.loads(raw) if raw.strip() else {}
            except ValueError:
                raise GraphError(FailureKind.UNKNOWN, "microsoft returned a body that is not JSON") from None
            if not isinstance(data, dict):
                break
            items += [x for x in data.get("value") or [] if isinstance(x, dict)]
            next_link = data.get("@odata.nextLink") or None
            delta_link = data.get("@odata.deltaLink") or delta_link
            for link in (next_link, delta_link):
                if link is not None and (not isinstance(link, str) or _bad_graph_url(link)):
                    raise GraphError(FailureKind.UNKNOWN, "microsoft returned a link outside Graph")
            if len(items) >= limit or not next_link or total >= self.max_json:
                break  # the aggregate byte budget ends paging early; next_link lets the caller continue
            target, query = next_link, None
        return items[:limit], next_link, delta_link
