# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Mavis AI's in-house Notion executor: search, read, create a page and append text.

Implements the provider-neutral action names in actions.py over NotionApi. Everything Notion returns is
third-party data; error text on a failed ToolResult is a status and vendor code only, the user-facing words
come from the FailureKind.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import ToolResult, UserRef
from mavis.tools.integrations.actions import ACTIONS, notion_paragraphs
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired, TokenSource
from mavis.tools.integrations.native.notion_api import (
    NotionApi,
    NotionError,
    notion_id,
    page_text,
    rich_plain,
)

ID_ARGS = ("page_id", "parent_page_id")


def title_of(obj: dict) -> str:
    """The title of a page (its title property) or a data source (its title)."""
    props = obj.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return rich_plain(prop.get("title")).strip()
        return ""
    return rich_plain(obj.get("title")).strip()


def paragraph_blocks(content: str) -> list[dict]:
    return [{"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": p}}]}}
            for p in notion_paragraphs(content)]


class NotionExecutor:
    provider = NativeProvider.NOTION

    def __init__(self, tokens: TokenSource, client: httpx.AsyncClient, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.api = NotionApi(tokens, client, sleep=sleep, clock=clock)
        self._handlers: dict[str, Callable[[int, Any], Awaitable[Any]]] = {
            "notion.search": self._search, "notion.read": self._read,
            "notion.create_page": self._create_page, "notion.append": self._append,
        }

    def forget_user(self, user_id: int) -> None:
        return None

    def handles(self, action: str) -> bool:
        return action in self._handlers

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        handler = self._handlers.get(action)
        if handler is None:
            return ToolResult(ok=False, error=f"notion does not implement {action!r}",
                              error_kind=FailureKind.UNKNOWN)
        model = ACTIONS[action].args_model
        try:
            parsed = model.model_validate(args)
        except ValidationError as exc:
            locs = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
            return ToolResult(ok=False, error=f"invalid arguments for {action}: {', '.join(locs)}",
                              error_kind=FailureKind.INVALID_ARGUMENT, error_field=locs[0] if locs else None)
        fixed: dict[str, str] = {}
        for name in ID_ARGS:  # an id becomes a URL path segment: only a real Notion id gets through
            value = getattr(parsed, name, None)
            if isinstance(value, str):
                clean = notion_id(value)
                if clean is None:
                    return ToolResult(ok=False, error=f"invalid arguments for {action}: {name}",
                                      error_kind=FailureKind.INVALID_ARGUMENT, error_field=name)
                fixed[name] = clean
        parsed = parsed.model_copy(update=fixed)
        try:
            return ToolResult(ok=True, data=await handler(user.user_id, parsed))
        except ReauthRequired as exc:
            return ToolResult(ok=False, error=f"notion grant unusable: {exc}", error_kind=FailureKind.AUTH)
        except NotionError as exc:
            return ToolResult(ok=False, error=str(exc)[:300], error_kind=exc.kind,
                              error_field=self._our_field(model, exc))

    @staticmethod
    def _our_field(model: type[BaseModel], exc: NotionError) -> str | None:
        if exc.kind is FailureKind.NOT_FOUND:
            return next((n for n in ID_ARGS if n in model.model_fields), None)
        return None

    async def _search(self, uid: int, a: Any) -> list[dict]:
        found = await self.api.search(uid, a.query, "page", a.limit)
        return [{"id": p.get("id", ""), "title": title_of(p), "url": p.get("url", ""), "kind": "page",
                 "edited": p.get("last_edited_time", "")} for p in found if not p.get("in_trash")]

    async def _read(self, uid: int, a: Any) -> dict:
        page = await self.api.page(uid, a.page_id)
        if page.get("in_trash"):
            raise NotionError(FailureKind.NOT_FOUND, "in_trash")
        blocks = await self.api.children(uid, a.page_id)
        return {"title": title_of(page), "url": page.get("url", ""), "text": page_text(blocks)}

    async def _create_page(self, uid: int, a: Any) -> dict:
        page = await self.api.create_page(
            uid, {"type": "page_id", "page_id": a.parent_page_id},
            {"title": {"title": [{"type": "text", "text": {"content": a.title}}]}},
            paragraph_blocks(a.content))
        return {"id": page.get("id", ""), "title": a.title, "url": page.get("url", "")}

    async def _append(self, uid: int, a: Any) -> dict:
        blocks = paragraph_blocks(a.content)
        if not blocks:
            raise NotionError(FailureKind.INVALID_ARGUMENT, "empty_content")
        await self.api.append(uid, a.page_id, blocks)
        return {"added": len(blocks)}
