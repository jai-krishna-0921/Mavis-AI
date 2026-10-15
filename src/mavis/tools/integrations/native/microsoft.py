# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Mavis AI's in-house Microsoft 365 executor (Outlook, Calendar, Teams, OneDrive) over Microsoft Graph.

Implements the provider-neutral action names in actions.py. Result `data` uses the key names
normalize.py and the renderers already read. Everything Graph returns is third-party data; error text on a
failed ToolResult is a status and error code only, the user-facing words come from the FailureKind.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ToolResult, UserRef
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired, TokenSource
from mavis.tools.integrations.native.graph import GRAPH, GraphClient, GraphError, bad_segment
from mavis.tools.integrations.native.graph_calendar import calendar_handlers
from mavis.tools.integrations.native.graph_mail import mail_handlers
from mavis.tools.integrations.native.graph_teams import teams_handlers

ID_ARGS = ("message_id", "thread_id", "event_id", "calendar_id", "chat_id", "item_id", "file_id",
           "message_ids", "thread_ids")
PRIMARY = "primary"


class MicrosoftExecutor:
    provider = NativeProvider.MICROSOFT

    def __init__(self, tokens: TokenSource, client: httpx.AsyncClient, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.graph = GraphClient(tokens, client, sleep=sleep)
        self._tokens = tokens
        self._handlers: dict[str, Callable[[int, Any], Awaitable[Any]]] = {"mail.profile": self._mail_profile}
        self._handlers.update(mail_handlers(self))
        self._handlers.update(calendar_handlers(self))
        self._handlers.update(teams_handlers(self))

    def handles(self, action: str) -> bool:
        return action in self._handlers

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        handler = self._handlers.get(action)
        if handler is None:
            return ToolResult(ok=False, error=f"microsoft does not implement {action!r}",
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
            if name == "calendar_id" and value == PRIMARY:
                continue
            bad = (any(bad_segment(v) for v in value) if isinstance(value, list)
                   else isinstance(value, str) and bad_segment(value))
            if bad:
                return ToolResult(ok=False, error=f"invalid arguments for {action}: {name}",
                                  error_kind=FailureKind.INVALID_ARGUMENT, error_field=name)
        try:
            return ToolResult(ok=True, data=await handler(user.user_id, parsed))
        except ReauthRequired as exc:
            return ToolResult(ok=False, error=f"microsoft grant unusable: {exc}", error_kind=FailureKind.AUTH)
        except GraphError as exc:
            return ToolResult(ok=False, error=exc.detail[:300], error_kind=exc.kind,
                              error_field=self._our_field(model, exc))

    @staticmethod
    def _our_field(model: type[BaseModel], exc: GraphError) -> str | None:
        fields = model.model_fields
        if exc.kind is FailureKind.NOT_FOUND:
            return next((n for n in ID_ARGS if n in fields), None)
        if exc.kind is FailureKind.INVALID_ARGUMENT and exc.field:
            segment = re.split(r"[.\[]", exc.field, maxsplit=1)[0]
            return segment if segment in fields else None
        return None

    async def _mail_profile(self, uid: int, a: Any) -> dict:
        me = await self.graph.json(uid, "GET", f"{GRAPH}/me", params={"$select": "mail,userPrincipalName"})
        return {"emailAddress": str(me.get("mail") or me.get("userPrincipalName") or "").lower()}
