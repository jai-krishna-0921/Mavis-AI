"""Best-effort presence cues (reaction, typing) around a user turn.

Everything here swallows errors: presence is cosmetic and must never block or fail a turn. Channels
that do not implement `react` (console, tests) are simply skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import structlog

from mavis.channels import get_channel
from mavis.config import get_settings

log = structlog.get_logger(__name__)

ACK_EMOJI = "\N{EYES}"
TYPING_REFRESH_S = 4.0
CALL_TIMEOUT_S = 3.0


async def react(chat_id: int, message_id: int | None, emoji: str | None = None) -> bool:
    """Set a reaction on the user's message; True when the channel accepted it. Never raises. The
    "seen" cue is off unless PRESENCE_REACTION is set (or an explicit emoji is passed)."""
    emoji = emoji or get_settings().presence_reaction
    if message_id is None or not emoji:
        return False
    return await _set_reaction(chat_id, message_id, emoji)


async def clear(chat_id: int, message_id: int | None) -> bool:
    """Remove the bot's reaction from the user's message. Never raises."""
    if message_id is None:
        return False
    return await _set_reaction(chat_id, message_id, None)


async def _set_reaction(chat_id: int, message_id: int, emoji: str | None) -> bool:
    try:
        fn = getattr(get_channel(), "react", None)
        if fn is None:
            return False
        await asyncio.wait_for(fn(chat_id, message_id, emoji), CALL_TIMEOUT_S)
        return True
    except Exception as exc:  # noqa: BLE001 - cosmetic
        log.warning("presence.react_failed", error=type(exc).__name__, clearing=emoji is None)
        return False


_acks: dict[str, asyncio.Task] = {}  # in-flight "seen" cues by event id


def track_ack(key: str, task: asyncio.Task) -> None:
    """Remember an in-flight "seen" cue so the turn's final reaction never lands before it."""
    _acks[key] = task
    task.add_done_callback(lambda _t: _acks.pop(key, None))


async def ack_settled(key: str) -> None:
    """Wait (briefly) for the "seen" cue of `key` to land. Never raises."""
    task = _acks.get(key)
    if task is not None and not task.done():
        await asyncio.wait({task}, timeout=CALL_TIMEOUT_S)


async def _send_typing(chat_id: int) -> None:
    try:
        await asyncio.wait_for(get_channel().send_typing(chat_id), CALL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - cosmetic
        log.debug("presence.typing_failed", error=type(exc).__name__)


@contextlib.asynccontextmanager
async def typing(chat_id: int | None, interval_s: float = TYPING_REFRESH_S) -> AsyncIterator[None]:
    """Show "typing" now and refresh it every `interval_s` until the block exits."""
    if chat_id is None:
        yield
        return

    async def _refresh() -> None:
        # first cue goes out immediately, but inside the task so a slow Telegram never delays the turn
        await _send_typing(chat_id)
        while True:
            await asyncio.sleep(interval_s)
            await _send_typing(chat_id)

    task = asyncio.create_task(_refresh())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
