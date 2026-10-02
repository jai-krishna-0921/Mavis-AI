"""Process-wide channel accessor. Business logic depends on the Channel protocol only."""

from __future__ import annotations

from zento.channels.base import Channel
from zento.config import get_settings

_channel: Channel | None = None


def get_channel() -> Channel:
    global _channel
    if _channel is None:
        token = get_settings().telegram_bot_token
        if token:
            from zento.channels.telegram import TelegramChannel

            _channel = TelegramChannel(token)
        else:
            from zento.channels.fake import ConsoleChannel

            _channel = ConsoleChannel()
    return _channel


def set_channel(channel: Channel | None) -> None:
    global _channel
    _channel = channel
