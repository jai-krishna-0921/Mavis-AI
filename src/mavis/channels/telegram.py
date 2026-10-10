"""Telegram adapter over python-telegram-bot's Bot. Only this module imports `telegram`."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    ReactionTypeEmoji,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, RetryAfter

from mavis.channels.base import ChannelRateLimited, MessageGone, RecipientUnreachable
from mavis.channels.formatting import to_plain, to_telegram_html
from mavis.channels.sent_log import note_sent
from mavis.channels.text import TELEGRAM_LIMIT, split_text
from mavis.domain.messages import Button

DELETE_BATCH = 100  # deleteMessages: 1-100 ids per call
_CHUNK_LIMIT = 3500  # leave room for HTML tags under Telegram's 4096 cap

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_NOT_MODIFIED = "not modified"
_GONE = ("message to edit not found", "message can't be edited", "message_id_invalid")

log = structlog.get_logger(__name__)


_GONE = ("chat not found", "user is deactivated", "bot was blocked", "bot was kicked", "peer_id_invalid")


def _unreachable(exc: Exception) -> bool:
    """Telegram's answer means the chat can never receive a message (blocked, deleted, never existed)."""
    return isinstance(exc, Forbidden) or any(g in str(exc).lower() for g in _GONE)


def _seconds(value: int | float | timedelta) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


def to_inline_button(b: Button) -> InlineKeyboardButton:
    if b.url:
        return InlineKeyboardButton(text=b.label, url=b.url)
    if not b.data:
        raise ValueError(f"button {b.label!r} has neither data nor url")
    return InlineKeyboardButton(text=b.label, callback_data=b.data)


def _fit_html(text: str) -> str:
    """Markdown to Telegram HTML within the message limit. Cuts the markdown first and converts the cut,
    so a tag is never sliced in half (HTML escapes can grow the text, so shrink until it fits)."""
    cut = text[:TELEGRAM_LIMIT]
    body = to_telegram_html(cut)
    while len(body) > TELEGRAM_LIMIT and len(cut) > 1:
        cut = cut[: max(1, int(len(cut) * 0.9))]
        body = to_telegram_html(cut)
    return body


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
            [[to_inline_button(b) for b in row] for row in buttons]
        )

    async def send_text(
        self, chat_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> list[int]:
        await self._ensure()
        chunks = split_text(text, _CHUNK_LIMIT)
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            markup = self._markup(buttons) if i == len(chunks) - 1 else None
            try:
                ids.extend(await self._send_markdown(chat_id, chunk, markup))
            except RetryAfter as exc:
                raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
            except (Forbidden, BadRequest) as exc:
                if _unreachable(exc):
                    raise RecipientUnreachable(str(exc)) from exc
                raise
        await note_sent(chat_id, ids)
        return ids

    async def _send_markdown(
        self, chat_id: int, chunk: str, markup: InlineKeyboardMarkup | None
    ) -> list[int]:
        """Send one markdown chunk; re-split smaller if the HTML is too long for Telegram."""
        body = to_telegram_html(chunk)
        if len(body) > TELEGRAM_LIMIT and len(chunk) > 1:
            return await self._send_halves(chat_id, chunk, markup)
        try:
            msg = await self._bot.send_message(
                chat_id=chat_id,
                text=body,
                parse_mode="HTML",
                link_preview_options=_NO_PREVIEW,
                reply_markup=markup,
            )
        except BadRequest as exc:
            message = str(exc).lower()
            if "too long" in message and len(chunk) > 1:
                return await self._send_halves(chat_id, chunk, markup)
            if "parse" not in message and "entities" not in message:
                raise
            log.warning("telegram.html_rejected", error=str(exc))
            msg = await self._bot.send_message(
                chat_id=chat_id,
                text=to_plain(chunk),
                link_preview_options=_NO_PREVIEW,
                reply_markup=markup,
            )
        return [msg.message_id]

    async def _send_halves(
        self, chat_id: int, chunk: str, markup: InlineKeyboardMarkup | None
    ) -> list[int]:
        parts = split_text(chunk, max(len(chunk) // 2, 1))
        ids: list[int] = []
        for i, part in enumerate(parts):
            ids.extend(await self._send_markdown(chat_id, part, markup if i == len(parts) - 1 else None))
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
        except (Forbidden, BadRequest) as exc:
            if _unreachable(exc):
                raise RecipientUnreachable(str(exc)) from exc
            raise
        await note_sent(chat_id, [msg.message_id])
        return msg.message_id

    async def edit_text(self, chat_id: int, message_id: int, text: str,
                        buttons: list[list[Button]] | None = None) -> None:
        await self._ensure()
        body = _fit_html(text)
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=body, parse_mode="HTML",
                link_preview_options=_NO_PREVIEW, reply_markup=self._markup(buttons),
            )
        except RetryAfter as exc:
            raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
        except BadRequest as exc:
            message = str(exc).lower()
            if _NOT_MODIFIED in message:
                return
            if any(g in message for g in _GONE):
                raise MessageGone(str(exc)) from exc
            raise

    async def send_photo(self, chat_id: int, path: str, caption: str = "") -> int:
        await self._ensure()
        try:
            with open(path, "rb") as fh:  # noqa: ASYNC230 - small local read handed to PTB
                msg = await self._bot.send_photo(chat_id=chat_id, photo=fh, caption=caption[:1024] or None)
        except RetryAfter as exc:
            raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
        except (Forbidden, BadRequest) as exc:
            if _unreachable(exc):
                raise RecipientUnreachable(str(exc)) from exc
            raise
        await note_sent(chat_id, [msg.message_id])
        return msg.message_id

    async def send_media_group(self, chat_id: int, paths: list[str],
                               captions: list[str] | None = None) -> list[int]:
        await self._ensure()
        caps = list(captions or [""] * len(paths))
        handles = [open(p, "rb") for p in paths[:10]]  # noqa: ASYNC230, SIM115 - closed below
        try:
            media = [InputMediaPhoto(media=h, caption=(c[:1024] or None))
                     for h, c in zip(handles, caps[: len(handles)], strict=True)]
            msgs = await self._bot.send_media_group(chat_id=chat_id, media=media)
        except RetryAfter as exc:
            raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
        finally:
            for h in handles:
                h.close()
        ids = [m.message_id for m in msgs]
        await note_sent(chat_id, ids)
        return ids

    async def send_typing(self, chat_id: int) -> None:
        await self._ensure()
        try:
            await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as exc:  # noqa: BLE001 - typing is cosmetic
            log.debug("telegram.typing_failed", error=type(exc).__name__)

    async def react(self, chat_id: int, message_id: int, emoji: str | None) -> None:
        """One emoji reaction (bots get one per message; it replaces the earlier one), or clear it."""
        await self._ensure()
        reaction = [ReactionTypeEmoji(emoji)] if emoji else []
        await self._bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=reaction)

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> bool:
        """deleteMessages takes 1-100 ids; unknown or too old ids are skipped by Telegram. A BadRequest
        (nothing in the batch can be deleted) is a refusal, not an error."""
        await self._ensure()
        ids = list(dict.fromkeys(int(m) for m in message_ids))
        if not ids or len(ids) > DELETE_BATCH:
            raise ValueError(f"deleteMessages takes 1 to {DELETE_BATCH} ids")
        try:
            return bool(await self._bot.delete_messages(chat_id=chat_id, message_ids=ids))
        except RetryAfter as exc:
            raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
        except BadRequest as exc:
            log.info("telegram.delete_refused", error=str(exc)[:120])
            return False

    async def leave_chat(self, chat_id: int) -> None:
        await self._ensure()
        await self._bot.leave_chat(chat_id=chat_id)

    async def download_file(self, file_id: str, dest_path: str) -> str:
        await self._ensure()
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        tg_file = await self._bot.get_file(file_id)
        await tg_file.download_to_drive(dest_path)
        return dest_path
