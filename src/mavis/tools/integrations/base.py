"""Integration provider port. Agents and tools depend on this, never on a vendor SDK."""

from __future__ import annotations

import json
from typing import Protocol

from mavis.domain.events import Event
from mavis.domain.integrations import ConnectionState, Toolkit, ToolResult, UserRef
from mavis.memory.extractor import wrap_untrusted

MAX_RESULT_CHARS = 6000


class IntegrationProvider(Protocol):
    async def catalog(self) -> list[Toolkit]: ...
    async def status(self, user: UserRef) -> dict[str, ConnectionState]: ...
    async def connect_link(self, user: UserRef, toolkit: str, callback_url: str) -> str: ...
    async def disconnect(self, user: UserRef, toolkit: str) -> None: ...
    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult: ...   # Mavis action name
    async def subscribe(self, user: UserRef, trigger: str, config: dict) -> str: ...      # Mavis trigger name
    async def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[Event]: ...


def render_result(result: ToolResult, limit: int = MAX_RESULT_CHARS) -> str:
    """The only way a provider result reaches a model: JSON text, truncated."""
    if not result.ok:
        # provider error text is third-party content: never let it read as instructions
        # truncate the inner text first so the closing tag is never cut off
        inner = (result.error or "unknown error")[: max(limit - 120, 0)]
        return f"error: {wrap_untrusted(inner, 'provider_error')}"
    data = result.data
    text = data if isinstance(data, str) else json.dumps(data, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " …[truncated]"
