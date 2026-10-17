# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Notion transport: one authorised, paced call with the retry rules, plus the helpers the executor and the
sync share.

Every call carries `Notion-Version` and the user's bearer token. Pacing is per user: a token bucket of 5
that refills at 2.5 per second (the free plan allows 180 requests a minute). A vendor error body is never
kept: only the status and the vendor `code` string survive, mapped to a FailureKind.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from mavis.domain.errors import FailureKind
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired, TokenSource
from mavis.tools.integrations.native.google import NEVER_SENT
from mavis.tools.integrations.native.http import API_BYTES, ResponseTooLarge, send_capped

NOTION_VERSION = "2026-03-11"
NOTION_BASE = "https://api.notion.com/v1"
BUCKET_CAPACITY = 5.0
REFILL_PER_S = 2.5
MAX_RETRY_WAIT_S = 60.0
DEFAULT_RETRY_WAIT_S = 1.0
BACKOFF_S = 1.0
PAGE_SIZE = 100
MAX_LIST_REQUESTS = 20  # a runaway cursor cannot loop forever
CHILDREN_PER_CALL = 100
READ_TEXT_CHARS = 20000
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_DASHED = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SEGMENT = re.compile(r"[A-Za-z0-9_\-]{1,64}")
_CODE_OK = re.compile(r"[a-z_]{1,64}")


def notion_id(value: str) -> str | None:
    """The dashed lowercase UUID for 32 hex characters (dashed or not), else None. Anything else (a URL, a
    path, a query suffix) is refused, so an id is always safe to use as one URL path segment."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if _DASHED.fullmatch(raw):
        raw = raw.replace("-", "")
    if not _HEX32.fullmatch(raw):
        return None
    h = raw.lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class NotionError(Exception):
    def __init__(self, kind: FailureKind, code: str = "", retry_after: float | None = None,
                 status: int = 0) -> None:
        text = " ".join(p for p in ("notion", str(status) if status else "", code) if p)
        super().__init__(text)
        self.kind, self.code, self.retry_after, self.status = kind, code, retry_after, status


def _seg(value: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value):
        raise NotionError(FailureKind.INVALID_ARGUMENT, "invalid_id")
    return value


def _error_code(resp: httpx.Response) -> str:
    try:
        code = resp.json().get("code")
    except (ValueError, AttributeError):
        return ""
    return code if isinstance(code, str) and _CODE_OK.fullmatch(code) else ""


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return max(0.0, float(resp.headers.get("retry-after", "")))
    except ValueError:
        return None


class NotionApi:
    def __init__(self, tokens: TokenSource, client: httpx.AsyncClient, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._tokens, self._client, self._sleep, self._clock = tokens, client, sleep, clock
        self._buckets: dict[int, list[float]] = {}  # user -> [tokens, last refill]
        self._locks: dict[int, asyncio.Lock] = {}

    def forget_user(self, user_id: int) -> None:
        self._buckets.pop(user_id, None)
        self._locks.pop(user_id, None)

    async def _pace(self, user_id: int) -> None:
        """Take one token from the user's bucket, sleeping until one exists. One waiter at a time per user,
        so concurrent callers queue in order. After a sleep the wait is booked as elapsed (no clock re-read),
        which keeps the loop finite under an injected sleep that does not move the clock."""
        async with self._locks.setdefault(user_id, asyncio.Lock()):
            now = self._clock()
            tokens, last = self._buckets.get(user_id, [BUCKET_CAPACITY, now])
            tokens = min(BUCKET_CAPACITY, tokens + max(0.0, now - last) * REFILL_PER_S)
            if tokens < 1.0:
                await self._sleep((1.0 - tokens) / REFILL_PER_S)
                tokens, now = 1.0, self._clock()
            self._buckets[user_id] = [tokens - 1.0, now]

    async def request(self, user_id: int, method: str, path: str, *, json: dict | None = None,
                      params: dict | None = None) -> dict:
        method = method.upper()
        token = await self._tokens.access_token(user_id, NativeProvider.NOTION)
        refreshed = throttled = conflicted = backed_off = False
        while True:
            await self._pace(user_id)
            headers = {"Notion-Version": NOTION_VERSION, "Authorization": f"Bearer {token}"}
            kw: dict[str, Any] = {"headers": headers, "params": params}
            if json is not None:
                kw["json"] = json
            try:
                resp = await send_capped(self._client, method, NOTION_BASE + path, max_bytes=API_BYTES, **kw)
            except ResponseTooLarge:
                raise NotionError(FailureKind.UNKNOWN, "response_too_large") from None
            except httpx.TransportError as exc:
                if method != "GET" and not isinstance(exc, NEVER_SENT):
                    raise NotionError(FailureKind.UNCONFIRMED, "transport") from None
                if method != "GET" or backed_off:
                    raise NotionError(FailureKind.UNAVAILABLE, "transport") from None
                backed_off = True
                await self._sleep(BACKOFF_S)
                continue
            status = resp.status_code
            if status < 300:
                try:
                    data = resp.json() if resp.content.strip() else {}
                except ValueError:
                    raise NotionError(FailureKind.UNKNOWN, "not_json", status=status) from None
                return data if isinstance(data, dict) else {}
            code = _error_code(resp)
            if status == 401:
                if refreshed:
                    raise ReauthRequired(f"notion {status} {code}".strip())
                token = await self._tokens.access_token(user_id, NativeProvider.NOTION, force=True)
                refreshed = True
                continue
            if status == 429:
                wait = _retry_after(resp)
                wait = DEFAULT_RETRY_WAIT_S if wait is None else min(wait, MAX_RETRY_WAIT_S)
                if throttled:
                    raise NotionError(FailureKind.RATE_LIMITED, code, wait, status)
                throttled = True
                await self._sleep(wait)
                continue
            if status == 409 and code == "conflict_error":
                if conflicted:
                    raise NotionError(FailureKind.UNAVAILABLE, code, status=status)
                conflicted = True
                await self._sleep(BACKOFF_S)
                continue
            if status >= 500:
                if method != "GET":
                    raise NotionError(FailureKind.UNCONFIRMED, code, status=status)
                if backed_off:
                    raise NotionError(FailureKind.UNAVAILABLE, code, status=status)
                backed_off = True
                await self._sleep(BACKOFF_S)
                continue
            raise NotionError(_kind_of(status), code, status=status)

    # ----- helpers (all paginate up to a caller cap)

    async def _paged(self, user_id: int, method: str, path: str, *, body: dict | None, limit: int,
                     params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_REQUESTS):
            size = max(1, min(PAGE_SIZE, limit - len(items)))
            if method == "GET":
                q = {**(params or {}), "page_size": size, **({"start_cursor": cursor} if cursor else {})}
                data = await self.request(user_id, "GET", path, params=q)
            else:
                b = {**(body or {}), "page_size": size, **({"start_cursor": cursor} if cursor else {})}
                data = await self.request(user_id, method, path, json=b)
            items += [x for x in data.get("results") or [] if isinstance(x, dict)]
            cursor = data.get("next_cursor") if data.get("has_more") else None
            if len(items) >= limit or not cursor:
                break
        return items[:limit]

    async def search(self, user_id: int, query: str, kind: str | None, limit: int) -> list[dict]:
        body: dict[str, Any] = {"query": query,
                                "sort": {"timestamp": "last_edited_time", "direction": "descending"}}
        if kind:
            body["filter"] = {"property": "object", "value": kind}
        return await self._paged(user_id, "POST", "/search", body=body, limit=limit)

    async def page(self, user_id: int, page_id: str) -> dict:
        return await self.request(user_id, "GET", f"/pages/{_seg(page_id)}")

    async def children(self, user_id: int, block_id: str, limit: int = 500) -> list[dict]:
        return await self._paged(user_id, "GET", f"/blocks/{_seg(block_id)}/children", body=None, limit=limit)

    async def append(self, user_id: int, block_id: str, blocks: list[dict], *,
                     position: dict | None = None) -> list[str]:
        """Add blocks in chunks of 100. With a `position` the first chunk goes there and each later chunk
        follows the last block added, so the order is kept. Returns the new block ids."""
        ids: list[str] = []
        pos = position
        for start in range(0, len(blocks), CHILDREN_PER_CALL):
            body: dict[str, Any] = {"children": blocks[start:start + CHILDREN_PER_CALL]}
            if pos is not None:
                body["position"] = pos
            data = await self.request(user_id, "PATCH", f"/blocks/{_seg(block_id)}/children", json=body)
            new = [b["id"] for b in data.get("results") or [] if isinstance(b, dict) and b.get("id")]
            ids += new
            if position is not None and new:
                pos = {"type": "after_block", "after_block": {"id": new[-1]}}
        return ids

    async def delete_block(self, user_id: int, block_id: str) -> None:
        await self.request(user_id, "DELETE", f"/blocks/{_seg(block_id)}")

    async def create_page(self, user_id: int, parent: dict, properties: dict,
                          children: Sequence[dict] = ()) -> dict:
        kids = list(children)
        body: dict[str, Any] = {"parent": parent, "properties": properties}
        if kids:
            body["children"] = kids[:CHILDREN_PER_CALL]
        page = await self.request(user_id, "POST", "/pages", json=body)
        if len(kids) > CHILDREN_PER_CALL and page.get("id"):
            await self.append(user_id, page["id"], kids[CHILDREN_PER_CALL:])
        return page

    async def update_page(self, user_id: int, page_id: str, *, properties: dict | None = None,
                          in_trash: bool | None = None) -> dict:
        body: dict[str, Any] = {}
        if properties is not None:
            body["properties"] = properties
        if in_trash is not None:
            body["in_trash"] = in_trash
        return await self.request(user_id, "PATCH", f"/pages/{_seg(page_id)}", json=body)

    async def create_database(self, user_id: int, page_id: str, title: str,
                              properties: dict, description: str = "") -> tuple[str, str]:
        body: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {"properties": properties},
        }
        if description:
            body["description"] = [{"type": "text", "text": {"content": description}}]
        db = await self.request(user_id, "POST", "/databases", json=body)
        sources = db.get("data_sources") or []
        ds_id = sources[0].get("id") if sources and isinstance(sources[0], dict) else None
        if not db.get("id") or not ds_id:
            raise NotionError(FailureKind.UNKNOWN, "no_data_source")
        return db["id"], ds_id

    async def update_data_source(self, user_id: int, ds_id: str, properties: dict) -> dict:
        return await self.request(user_id, "PATCH", f"/data_sources/{_seg(ds_id)}",
                                  json={"properties": properties})

    async def database(self, user_id: int, db_id: str) -> dict:
        return await self.request(user_id, "GET", f"/databases/{_seg(db_id)}")

    async def data_source(self, user_id: int, ds_id: str) -> dict:
        return await self.request(user_id, "GET", f"/data_sources/{_seg(ds_id)}")

    async def query(self, user_id: int, ds_id: str, *, filter: dict | None = None,  # noqa: A002
                    sorts: list | None = None, limit: int = 100) -> list[dict]:
        body: dict[str, Any] = {}
        if filter is not None:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        path = f"/data_sources/{_seg(ds_id)}/query"
        return await self._paged(user_id, "POST", path, body=body, limit=limit)


def _kind_of(status: int) -> FailureKind:
    if status == 403:
        return FailureKind.PERMISSION_MISSING
    if status in (404, 410):
        return FailureKind.NOT_FOUND
    if status in (400, 413, 422):
        return FailureKind.INVALID_ARGUMENT
    return FailureKind.UNKNOWN


# ----- reading blocks as text

def rich_plain(rich: Any) -> str:
    if not isinstance(rich, list):
        return ""
    return "".join(str(r.get("plain_text") or (r.get("text") or {}).get("content") or "")
                   for r in rich if isinstance(r, dict))


_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "bulleted_list_item": "- ",
           "numbered_list_item": "- ", "quote": "> ", "toggle": "- "}
_TEXT_TYPES = ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
               "numbered_list_item", "quote", "callout", "toggle")


def page_text(blocks: list[dict], limit: int = READ_TEXT_CHARS) -> str:
    """Plain text of paragraph, heading, list, to_do, quote, callout, toggle and code blocks. A child page
    is only named; every other block type is left out."""
    lines: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        inner = b.get(kind) if isinstance(kind, str) else None
        inner = inner if isinstance(inner, dict) else {}
        if kind in _TEXT_TYPES:
            text = rich_plain(inner.get("rich_text"))
            line = _PREFIX.get(kind, "") + text
        elif kind == "to_do":
            line = ("[x] " if inner.get("checked") else "[ ] ") + rich_plain(inner.get("rich_text"))
        elif kind == "code":
            line = "```\n" + rich_plain(inner.get("rich_text")) + "\n```"
        elif kind == "child_page":
            line = f"[page: {str(inner.get('title') or '').strip()}]"
        else:
            continue
        if line.strip():
            lines.append(line)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit].rstrip()
