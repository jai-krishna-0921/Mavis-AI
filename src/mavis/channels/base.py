from __future__ import annotations

from typing import Protocol

from mavis.domain.messages import Button


class ChannelRateLimited(Exception):
    """The provider asked us to slow down; retry after `retry_after` seconds (not a delivery failure)."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class Channel(Protocol):
    async def send_text(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> list[int]:
        """Send text (split at 4096 chars). Returns provider message ids."""

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int: ...

    async def send_typing(self, chat_id: int) -> None: ...

    async def react(self, chat_id: int, message_id: int, emoji: str | None) -> None:
        """Set the bot's reaction on a user message (replacing any earlier one); None clears it. Best
        effort; callers swallow failures."""

    async def download_file(self, file_id: str, dest_path: str) -> str: ...
