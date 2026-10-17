# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Fathom transport: one authorised, paced GET with the retry rules, plus the meeting helpers.

Auth is the user's bearer token. Pacing is per user: 50 requests a minute overall and 25 a minute for
`/recordings/*`, each a token bucket that starts full. A vendor error body is never kept: only the status
and a short `error` code survive, mapped to a FailureKind. Everything is read-only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from mavis.domain.errors import FailureKind
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired, TokenSource
from mavis.tools.integrations.native.http import API_BYTES, ResponseTooLarge, send_capped

FATHOM_BASE = "https://api.fathom.ai/external/v1"
OVERALL_PER_MIN = 50.0
RECORDINGS_PER_MIN = 25.0
MAX_RETRY_WAIT_S = 60.0
DEFAULT_RETRY_WAIT_S = 1.0
BACKOFF_S = 1.0
MAX_LIST_REQUESTS = 20  # a runaway cursor cannot loop forever
FIND_REQUESTS = 3  # pages scanned to find one meeting's details
_CODE_OK = re.compile(r"[A-Za-z_]{1,64}")
WEBHOOK_TOLERANCE_S = 300


def verify_fathom_signature(secret: str, headers: Mapping[str, str], body: bytes, now: float) -> bool:
    """Standard Webhooks: HMAC-SHA256 over `{webhook-id}.{webhook-timestamp}.{body}` keyed by the base64
    part of the `whsec_` secret. The timestamp must be within five minutes of `now`; the signature header
    holds one or more space separated `v1,<base64>` values and any one may match. Never raises."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    wid, ts, sigs = (lowered.get(k, "") for k in ("webhook-id", "webhook-timestamp", "webhook-signature"))
    if not (wid and ts.isdigit() and sigs and secret):
        return False
    if abs(now - int(ts)) > WEBHOOK_TOLERANCE_S:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError):
        return False
    mac = hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode()
    ok = False
    for part in sigs.split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected.encode()):
            ok = True  # keep going: the time spent does not depend on which one matched
    return ok


class FathomError(Exception):
    def __init__(self, kind: FailureKind, code: str = "", retry_after: float | None = None,
                 status: int = 0) -> None:
        text = " ".join(p for p in ("fathom", str(status) if status else "", code) if p)
        super().__init__(text)
        self.kind, self.code, self.retry_after, self.status = kind, code, retry_after, status


def _error_code(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        code = body.get("error") or body.get("code")
    except (ValueError, AttributeError):
        return ""
    return code if isinstance(code, str) and _CODE_OK.fullmatch(code) else ""


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return max(0.0, float(resp.headers.get("retry-after", "")))
    except ValueError:
        return None


def _kind_of(status: int) -> FailureKind:
    if status == 403:
        return FailureKind.PERMISSION_MISSING
    if status in (404, 410):
        return FailureKind.NOT_FOUND
    if status in (400, 413, 422):
        return FailureKind.INVALID_ARGUMENT
    return FailureKind.UNKNOWN


def _rid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FathomError(FailureKind.INVALID_ARGUMENT, "invalid_id")
    return value


class FathomApi:
    def __init__(self, tokens: TokenSource, client: httpx.AsyncClient, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._tokens, self._client, self._sleep, self._clock = tokens, client, sleep, clock
        self._buckets: dict[int, dict[str, list[float]]] = {}  # user -> bucket name -> [tokens, last]
        self._locks: dict[int, asyncio.Lock] = {}

    def forget_user(self, user_id: int) -> None:
        self._buckets.pop(user_id, None)
        self._locks.pop(user_id, None)

    async def _pace(self, user_id: int, recordings: bool) -> None:
        """Take one token from each bucket that applies, sleeping until both have one. One waiter at a time
        per user, so callers queue in order. After a sleep the wait is booked as elapsed (no clock re-read),
        which keeps the loop finite under an injected sleep that does not move the clock."""
        rates = {"all": OVERALL_PER_MIN} | ({"rec": RECORDINGS_PER_MIN} if recordings else {})
        async with self._locks.setdefault(user_id, asyncio.Lock()):
            now = self._clock()
            mine = self._buckets.setdefault(user_id, {})
            level: dict[str, float] = {}
            wait = 0.0
            for name, per_min in rates.items():
                tokens, last = mine.get(name, [per_min, now])
                tokens = min(per_min, tokens + max(0.0, now - last) * per_min / 60.0)
                level[name] = tokens
                if tokens < 1.0:
                    wait = max(wait, (1.0 - tokens) * 60.0 / per_min)
            if wait:
                await self._sleep(wait)
            for name, per_min in rates.items():
                refilled = min(per_min, level[name] + wait * per_min / 60.0)
                mine[name] = [refilled - 1.0, now + wait if wait else now]
            if wait:
                now = self._clock()
                for name in rates:
                    mine[name][1] = now

    async def get(self, user_id: int, path: str, *, params: dict | None = None) -> dict:
        token = await self._tokens.access_token(user_id, NativeProvider.FATHOM)
        refreshed = throttled = backed_off = False
        recordings = path.startswith("/recordings/")
        while True:
            await self._pace(user_id, recordings)
            headers = {"Authorization": f"Bearer {token}"}
            try:
                resp = await send_capped(self._client, "GET", FATHOM_BASE + path, max_bytes=API_BYTES,
                                         headers=headers, params=params)
            except ResponseTooLarge:
                raise FathomError(FailureKind.UNKNOWN, "response_too_large") from None
            except httpx.TransportError:
                if backed_off:
                    raise FathomError(FailureKind.UNAVAILABLE, "transport") from None
                backed_off = True
                await self._sleep(BACKOFF_S)
                continue
            status = resp.status_code
            if status < 300:
                try:
                    data = resp.json() if resp.content.strip() else {}
                except ValueError:
                    raise FathomError(FailureKind.UNKNOWN, "not_json", status=status) from None
                return data if isinstance(data, dict) else {}
            code = _error_code(resp)
            if status == 401:
                if refreshed:
                    raise ReauthRequired(f"fathom {status} {code}".strip())
                token = await self._tokens.access_token(user_id, NativeProvider.FATHOM, force=True)
                refreshed = True
                continue
            if status == 429:
                wait = _retry_after(resp)
                wait = DEFAULT_RETRY_WAIT_S if wait is None else min(wait, MAX_RETRY_WAIT_S)
                if throttled:
                    raise FathomError(FailureKind.RATE_LIMITED, code, wait, status)
                throttled = True
                await self._sleep(wait)
                continue
            if status >= 500:
                if backed_off:
                    raise FathomError(FailureKind.UNAVAILABLE, code, status=status)
                backed_off = True
                await self._sleep(BACKOFF_S)
                continue
            raise FathomError(_kind_of(status), code, status=status)

    # ----- helpers

    async def meetings(self, user_id: int, *, created_after: datetime | None, limit: int) -> list[dict]:
        """Newest meetings first as the API returns them, with action items, following `next_cursor` until
        `limit` are collected. The list never carries summaries or transcripts for OAuth apps."""
        items: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_REQUESTS):
            params: dict[str, Any] = {"include_action_items": "true"}
            if created_after is not None:
                params["created_after"] = created_after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if cursor:
                params["cursor"] = cursor
            data = await self.get(user_id, "/meetings", params=params)
            items += [m for m in data.get("items") or [] if isinstance(m, dict)]
            nxt = data.get("next_cursor")
            cursor = nxt if isinstance(nxt, str) and nxt else None
            if len(items) >= limit or not cursor:
                break
        return items[:limit]

    async def find_meeting(self, user_id: int, recording_id: int) -> dict | None:
        """Details of one meeting (the API has no get-one): scan the recent list for it."""
        rid = _rid(recording_id)
        cursor: str | None = None
        for _ in range(FIND_REQUESTS):
            params: dict[str, Any] = {"include_action_items": "true"}
            if cursor:
                params["cursor"] = cursor
            data = await self.get(user_id, "/meetings", params=params)
            for m in data.get("items") or []:
                if isinstance(m, dict) and m.get("recording_id") == rid:
                    return m
            nxt = data.get("next_cursor")
            cursor = nxt if isinstance(nxt, str) and nxt else None
            if not cursor:
                break
        return None

    async def summary(self, user_id: int, recording_id: int) -> str:
        data = await self.get(user_id, f"/recordings/{_rid(recording_id)}/summary")
        summary = data.get("summary")
        text = summary.get("markdown_formatted") if isinstance(summary, dict) else None
        return text if isinstance(text, str) else ""

    async def transcript(self, user_id: int, recording_id: int, limit_chars: int = 30000) -> str:
        data = await self.get(user_id, f"/recordings/{_rid(recording_id)}/transcript")
        lines: list[str] = []
        for seg in data.get("transcript") or []:
            if not isinstance(seg, dict):
                continue
            speaker = seg.get("speaker")
            who = speaker.get("display_name") if isinstance(speaker, dict) else None
            text = str(seg.get("text") or "").strip()
            if text:
                lines.append(f"{who or 'Unknown'}: {text}")
        return "\n".join(lines)[:limit_chars]
