"""Process-wide channel accessor. Business logic depends on the Channel protocol only."""

from __future__ import annotations

from mavis.channels.base import Channel
from mavis.config import get_settings

_channel: Channel | None = None


def get_channel() -> Channel:
    global _channel
    if _channel is None:
        s = get_settings()
        if s.telegram_bot_token:
            from mavis.channels.telegram import TelegramChannel

            _channel = TelegramChannel(s.telegram_bot_token)
        else:
            from mavis.channels.fake import ConsoleChannel

            _channel = ConsoleChannel()
        from mavis.channels.test_sink import SinkChannel, active_test_chat

        if (test_chat := active_test_chat(s)) is not None:  # the live test chat never reaches Telegram
            _channel = SinkChannel(_channel, test_chat)
    return _channel


def set_channel(channel: Channel | None) -> None:
    global _channel
    _channel = channel
