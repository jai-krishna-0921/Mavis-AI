# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Mavis AI's in-house Fathom executor: list recent meetings and read one (summary and action items).

Read-only. Everything Fathom returns is third-party data; error text on a failed ToolResult is a status and
vendor code only, the user-facing words come from the FailureKind.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ToolResult, UserRef
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired, TokenSource
from mavis.tools.integrations.native.fathom_api import FathomApi, FathomError


def _start(meeting: dict) -> str:
    return str(meeting.get("recording_start_time") or meeting.get("scheduled_start_time") or "")


def _attendees(meeting: dict) -> list[str]:
    out = []
    for inv in meeting.get("calendar_invitees") or []:
        if isinstance(inv, dict):
            name = inv.get("name") or inv.get("matched_speaker_display_name") or inv.get("email")
            if name:
                out.append(str(name))
    return out


def _assignee(item: dict) -> str:
    who = item.get("assignee")
    if isinstance(who, dict):
        return str(who.get("name") or who.get("email") or "")
    return ""


class FathomExecutor:
    provider = NativeProvider.FATHOM

    def __init__(self, tokens: TokenSource, client: httpx.AsyncClient, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.api = FathomApi(tokens, client, sleep=sleep, clock=clock)
        self._handlers: dict[str, Callable[[int, Any], Awaitable[Any]]] = {
            "fathom.meetings": self._meetings, "fathom.meeting": self._meeting,
        }

    def forget_user(self, user_id: int) -> None:
        self.api.forget_user(user_id)

    def handles(self, action: str) -> bool:
        return action in self._handlers

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        handler = self._handlers.get(action)
        if handler is None:
            return ToolResult(ok=False, error=f"fathom does not implement {action!r}",
                              error_kind=FailureKind.UNKNOWN)
        try:
            parsed = ACTIONS[action].args_model.model_validate(args)
        except ValidationError as exc:
            locs = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
            return ToolResult(ok=False, error=f"invalid arguments for {action}: {', '.join(locs)}",
                              error_kind=FailureKind.INVALID_ARGUMENT, error_field=locs[0] if locs else None)
        try:
            return ToolResult(ok=True, data=await handler(user.user_id, parsed))
        except ReauthRequired as exc:
            return ToolResult(ok=False, error=f"fathom grant unusable: {exc}", error_kind=FailureKind.AUTH)
        except FathomError as exc:
            field = "recording_id" if exc.kind in (FailureKind.NOT_FOUND, FailureKind.INVALID_ARGUMENT) \
                and action == "fathom.meeting" else None
            return ToolResult(ok=False, error=str(exc)[:300], error_kind=exc.kind, error_field=field)

    async def _meetings(self, uid: int, a: Any) -> list[dict]:
        since = datetime.now(UTC) - timedelta(days=a.days)
        found = await self.api.meetings(uid, created_after=since, limit=a.limit)
        return [{"recording_id": m.get("recording_id"), "title": str(m.get("title") or ""),
                 "start": _start(m), "attendees": _attendees(m),
                 "action_items_count": len(m.get("action_items") or []), "url": str(m.get("url") or "")}
                for m in found]

    async def _meeting(self, uid: int, a: Any) -> dict:
        summary = await self.api.summary(uid, a.recording_id)  # a 404 here means no such recording
        meta = await self.api.find_meeting(uid, a.recording_id) or {}
        items = [{"description": str(i.get("description") or ""), "assignee": _assignee(i),
                  "completed": bool(i.get("completed"))}
                 for i in meta.get("action_items") or [] if isinstance(i, dict)]
        return {"title": str(meta.get("title") or ""), "start": _start(meta), "summary": summary,
                "action_items": items, "url": str(meta.get("url") or "")}
