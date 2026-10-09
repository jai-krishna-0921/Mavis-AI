"""Which channel a message to the user goes through, and the Slack channel accessor.

A chat handle's type names its channel: an int is a Telegram chat, `slack:<team>:<channel>[:<thread>]` is
Slack.
Rules for a queued message (`destinations`):
- it carries a route (queued during a Slack turn): reply there; a message with buttons (an approval card, a
  progress card) never goes to a shared channel or thread, it goes to the user's private DM instead;
- otherwise a proactive message follows the user's preference (telegram | slack | both, default telegram);
- anything else (a task result, an approval prompt queued by a background task) goes where the user last
  wrote from (telegram unless their last message came from Slack);
- a Slack destination that cannot be reached (no bot, no DM) falls back to Telegram, so nothing is lost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from mavis.channels import get_channel
from mavis.channels.base import Channel
from mavis.channels.slack import SlackChannel, chat_id, in_dm, is_slack_chat
from mavis.channels.turn_route import reply_chat

if TYPE_CHECKING:
    from mavis.domain.events import Event
    from mavis.store.models import User

log = structlog.get_logger(__name__)

PREFS = ("telegram", "slack", "both")
DEFAULT_PREF = "telegram"
SLACK_SOURCE = "slack_chat"

_slack: Channel | None = None


def get_slack_channel() -> Channel:
    global _slack
    if _slack is None:
        from mavis.config import get_settings

        async def token_for_team(team: str) -> str | None:
            from mavis.tools.integrations import get_provider

            find = getattr(getattr(get_provider(), "tokens", None), "bot_token_for_team", None)
            return await find(team) if find is not None else None

        _slack = SlackChannel(token_for_team, presence_emoji=lambda: get_settings().presence_reaction)
    return _slack


def set_slack_channel(channel: Channel | None) -> None:
    global _slack
    _slack = channel


def channel_for(chat: Any) -> Channel:
    return get_slack_channel() if is_slack_chat(chat) else get_channel()


def pref_of(state: dict | None) -> str:
    value = (state or {}).get("channel_pref")
    return value if value in PREFS else DEFAULT_PREF


async def set_pref(user_id: int, value: str) -> None:
    from mavis.store.repo import users

    await users.update_state(user_id, {"channel_pref": value})


def bind(event: Event):
    """Route this turn's queued messages to the channel the event came from (Slack only)."""
    chat = event.payload.get("reply_chat") if event.source == SLACK_SOURCE else None
    return reply_chat.set(chat if is_slack_chat(chat) else None)


def unbind(token) -> None:
    reply_chat.reset(token)


def turn_chat(event: Event, user: User) -> Any:
    """The chat this turn's cues (typing, reactions) go to."""
    if event.source == SLACK_SOURCE:
        chat = event.payload.get("reply_chat")
        return chat if is_slack_chat(chat) else None
    return user.telegram_chat_id


async def note_inbound(user: User, event: Event) -> None:
    """Remember which channel the user last wrote from. Best effort, one write only when it changes."""
    last = "slack" if event.source == SLACK_SOURCE else "telegram" if event.source == "telegram" else None
    if last is None or (user.state or {}).get("last_channel") == last:
        return
    try:
        from mavis.store.repo import users

        await users.update_state(user.id, {"last_channel": last})
    except Exception as exc:  # noqa: BLE001 - routing memory is a convenience
        log.warning("routing.note_failed", error=type(exc).__name__)


async def slack_chat_for(user_id: int) -> str | None:
    """The user's private DM with the bot, or None when Slack chat is not set up or cannot be reached."""
    try:
        from mavis.tools.integrations import get_provider
        from mavis.tools.integrations.native.base import NativeProvider

        tokens = getattr(get_provider(), "tokens", None)
        if tokens is None:
            return None
        who = await tokens.account(user_id, NativeProvider.SLACK)
        bot = await tokens.account(user_id, NativeProvider.SLACK_BOT)
        if not who or not bot:
            return None
        team = str(bot.get("team_id") or "")
        dm = str(bot.get("dm") or "")
        if not dm:
            dm = await get_slack_channel().open_dm(team, str(who.get("user_id") or ""))  # type: ignore[attr-defined]
        return chat_id(team, dm) if team and dm else None
    except Exception as exc:  # noqa: BLE001
        log.warning("routing.slack_unreachable", error=type(exc).__name__)
        return None


async def destinations(user: User, *, route: str | None, proactive: bool, private: bool) -> list[Any]:
    telegram = user.telegram_chat_id
    if route and is_slack_chat(route):
        if private and not in_dm(route):
            route = await slack_chat_for(user.id)
        if route:
            return [route]
    state = user.state or {}
    if proactive:
        want = pref_of(state)
    else:
        want = "slack" if state.get("last_channel") == "slack" else "telegram"
    out: list[Any] = []
    if want in ("telegram", "both") and telegram is not None:
        out.append(telegram)
    if want in ("slack", "both") and (slack := await slack_chat_for(user.id)):
        out.append(slack)
    if not out and telegram is not None:
        out.append(telegram)
    return out
