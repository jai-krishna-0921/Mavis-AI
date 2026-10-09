"""Where the reply of the current turn goes: set by the worker around an event from a non-Telegram channel,
read by the outbox when a message is queued. Telegram turns leave it unset (the default route)."""

from __future__ import annotations

from contextvars import ContextVar

reply_chat: ContextVar[str | None] = ContextVar("reply_chat", default=None)
