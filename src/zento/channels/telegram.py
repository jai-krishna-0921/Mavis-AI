"""Telegram adapter over python-telegram-bot's Bot. Only this module imports `telegram`."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import RetryAfter

from zento.channels.base import ChannelRateLimited
from zento.channels.text import split_text
from zento.domain.messages import Button

log = structlog.get_logger(__name__)


def _seconds(value: int | float | timedelta) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


class TelegramChannel:
    def __init__(self, token: str, bot: Any | None = None) -> None:
        self._bot = bot or Bot(token)
        self._ready = False

    async def _ensure(self) -> None:
        if not self._ready:
            await self._bot.initialize()
            self._ready = True

    @staticmethod
    def _markup(buttons: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
        if not buttons:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(b.label, callback_data=b.data) for b in row] for row in buttons]
        )

    async def send_text(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> list[int]:
        await self._ensure()
        chunks = split_text(text)
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            markup = self._markup(buttons) if i == len(chunks) - 1 else None
            try:
                msg = await self._bot.send_message(chat_id=chat_id, text=chunk, reply_markup=markup)
            except RetryAfter as exc:
                raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
            ids.append(msg.message_id)
        return ids

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int:
        await self._ensure()
        try:
            with open(path, "rb") as fh:  # noqa: ASYNC230 - small local read handed to PTB
                msg = await self._bot.send_document(
                    chat_id=chat_id, document=fh, filename=Path(path).name, caption=caption[:1024] or None
                )
        except RetryAfter as exc:
            raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
        return msg.message_id

    async def send_typing(self, chat_id: int) -> None:
        await self._ensure()
        try:
            await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as exc:  # noqa: BLE001 - typing is cosmetic
            log.debug("telegram.typing_failed", error=type(exc).__name__)

    async def download_file(self, file_id: str, dest_path: str) -> str:
        await self._ensure()
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        tg_file = await self._bot.get_file(file_id)
        await tg_file.download_to_drive(dest_path)
        return dest_path
