from __future__ import annotations

from typing import Protocol

from mavis.domain.messages import Button


class ChannelRateLimited(Exception):
    """The provider asked us to slow down; retry after `retry_after` seconds (not a delivery failure)."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class RecipientUnreachable(Exception):
    """The chat can never receive this: it does not exist, or the user blocked the bot. Not retried."""


class MessageGone(Exception):
    """The message to edit no longer exists (the user deleted it, or it is too old to edit)."""


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

    async def leave_chat(self, chat_id: int) -> None:
        """Leave a group or channel the bot was added to (Phase 11: private chats only)."""
    async def edit_text(self, chat_id: int, message_id: int, text: str,
                        buttons: list[list[Button]] | None = None) -> None:
        """Replace a message's text and keyboard (None removes it). Unchanged text is success.
        Raises MessageGone when the message cannot be edited any more, ChannelRateLimited on 429."""

    async def send_photo(self, chat_id: int, path: str, caption: str = "") -> int: ...

    async def send_media_group(self, chat_id: int, paths: list[str],
                               captions: list[str] | None = None) -> list[int]: ...

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> bool:
        """Delete up to 100 messages in one call (Telegram deleteMessages). Ids that no longer exist or are
        too old are skipped by the provider. Returns False when the provider refused the whole batch (or the
        channel cannot delete); raises ChannelRateLimited on 429."""
