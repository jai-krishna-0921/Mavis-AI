"""Slack chat intake: DMs to the Mavis bot and @mentions become the same USER_MESSAGE events Telegram makes.

The webhook has already verified the signature and de-duplicated the delivery. Rules, all structural:
- only `message` in a DM with the bot (channel_type im) and `app_mention` are chat; everything else keeps
  its old path (third-party records);
- the bot's own posts, other bots and system subtypes are dropped (no loops);
- the sender must map to a Mavis user through the native Slack grant (workspace + Slack user); a stranger
  gets one plain reply and nothing else happens;
- the reply route is the DM, or the thread of the mention.
"""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from mavis.bus.base import EventBus
from mavis.channels import routing
from mavis.channels.slack import chat_id, ts_to_id
from mavis.domain.events import Event, EventType, Trust

log = structlog.get_logger(__name__)

CHAT_SUBTYPES = frozenset({"file_share", "thread_broadcast"})
UNKNOWN_REPLY = ("I don't know you yet. Mavis is by invite: join on Telegram with your invite link, send "
                 "/connect slack there, and then message me here again.")
UNKNOWN_REPLY_EVERY_S = 3600
_COMMAND = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@\w+)?(?:\s|$)")
_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_LINK = re.compile(r"<((?:https?|mailto):[^>|]+)(?:\|([^>]*))?>")
_CHANNEL_REF = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")
_told: dict[tuple[str, str], float] = {}


def clean_text(text: str, bot_user_id: str = "") -> str:
    """Slack's wire text to what the user typed: our own @mention removed, links and entities unwrapped."""
    def mention(m: re.Match[str]) -> str:
        return "" if bot_user_id and m.group(1) == bot_user_id else m.group(0)

    text = _MENTION.sub(mention, text or "")
    text = _CHANNEL_REF.sub(lambda m: f"#{m.group(1)}", text)
    text = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), text)
    lines = [" ".join(line.split()) for line in html.unescape(text).split("\n")]
    return "\n".join(lines).strip()


async def is_chat_event(payload: dict, ev: dict, lookup: Any) -> bool:
    """A DM with the bot or an @mention. A user-token delivery of the same DM is recognised by the channel
    the bot noted at connect, so it is never also stored as a third-party record."""
    kind = ev.get("type")
    if kind == "app_mention":
        return True
    if kind != "message" or ev.get("channel_type") != "im":
        return False
    if any(isinstance(a, dict) and a.get("is_bot") for a in payload.get("authorizations") or []):
        return True
    owner = getattr(lookup, "bot_dm_owner", None)
    team = str(payload.get("team_id") or ev.get("team") or "")
    return bool(owner is not None and await owner(team, str(ev.get("channel") or "")) is not None)


def _ignored(ev: dict, bot_user_id: str) -> bool:
    if ev.get("bot_id") or ev.get("bot_profile") or ev.get("hidden"):
        return True
    subtype = ev.get("subtype")
    if subtype and subtype not in CHAT_SUBTYPES:
        return True
    sender = str(ev.get("user") or "")
    return not sender or (bool(bot_user_id) and sender == bot_user_id) or not ev.get("ts")


async def _refuse_unknown(chat: str, team: str, sender: str) -> None:
    key = (team, sender)
    now = time.monotonic()
    if now - _told.get(key, -UNKNOWN_REPLY_EVERY_S) < UNKNOWN_REPLY_EVERY_S:
        return
    if len(_told) > 5000:
        _told.clear()
    _told[key] = now
    try:
        await routing.get_slack_channel().send_text(chat, UNKNOWN_REPLY)
    except Exception as exc:  # noqa: BLE001 - a refusal that cannot be delivered is still a refusal
        log.warning("slack.unknown_reply_failed", error=type(exc).__name__)


async def handle(payload: dict, ev: dict, lookup: Any, bus: EventBus) -> dict[str, int]:
    counts = {"published": 0, "skipped": 0, "duplicate": 0, "unmapped": 0}
    team = str(payload.get("team_id") or ev.get("team") or "")
    channel = str(ev.get("channel") or "")
    bot_find = getattr(lookup, "bot_account", None)
    bot = await bot_find(team) if bot_find is not None else None
    bot_user_id = str((bot or {}).get("bot_user_id") or "")
    if not channel or _ignored(ev, bot_user_id):
        counts["skipped"] = 1
        return counts
    text = clean_text(str(ev.get("text") or ""), bot_user_id)
    if not text:
        counts["skipped"] = 1
        return counts
    ts = str(ev["ts"])
    mention = ev.get("type") == "app_mention"
    chat = chat_id(team, channel, str(ev.get("thread_ts") or ts) if mention else None)
    sender = str(ev["user"])
    user_id = await lookup.user_for_slack(team, sender) if lookup is not None else None
    if user_id is None:
        counts["unmapped"] = 1
        await _refuse_unknown(chat, team, sender)
        return counts
    body: dict[str, Any] = {"text": text, "message_id": ts_to_id(ts), "reply_chat": chat}
    if m := _COMMAND.match(text):
        body["command"] = m.group(1).lower()
    try:
        occurred = datetime.fromtimestamp(float(ts), UTC)
    except (ValueError, OverflowError, OSError):
        occurred = datetime.now(UTC)
    event = Event(id=f"slack:chat:{team}:{channel}:{ts}", user_id=user_id, type=EventType.USER_MESSAGE,
                  occurred_at=occurred, source=routing.SLACK_SOURCE, payload=body, trust=Trust.USER)
    if await bus.publish(event):
        counts["published"] = 1
    else:
        counts["duplicate"] = 1
    return counts
