"""Extra prompt context for a chat turn, contributed by other packages (the attention inbox digest, ...).

Each provider gets (user_id, user_text) and returns a block or "". Providers run concurrently under a short
timeout; a slow or failing provider contributes nothing and never delays or breaks the reply.
Phase 4's conversation.py must call gather_context() in its context assembly too."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)

ContextProvider = Callable[[int, str], Awaitable[str]]
CONTEXT_PROVIDERS: list[ContextProvider] = []
PROVIDER_TIMEOUT_S = 0.8


def register_context_provider(fn: ContextProvider) -> None:
    if fn not in CONTEXT_PROVIDERS:
        CONTEXT_PROVIDERS.append(fn)


def clear_context_providers() -> None:
    CONTEXT_PROVIDERS.clear()


async def _one(fn: ContextProvider, user_id: int, text: str) -> str:
    try:
        return await asyncio.wait_for(fn(user_id, text), PROVIDER_TIMEOUT_S) or ""
    except Exception as exc:  # noqa: BLE001 - includes TimeoutError: context is optional
        log.warning(
            "context.provider_failed", provider=getattr(fn, "__qualname__", "?"), error=type(exc).__name__
        )
        return ""


async def gather_context(user_id: int, text: str) -> str:
    if not CONTEXT_PROVIDERS:
        return ""
    parts = await asyncio.gather(*(_one(fn, user_id, text) for fn in list(CONTEXT_PROVIDERS)))
    return "\n\n".join(p for p in parts if p.strip())
