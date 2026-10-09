"""Slack adapter: Mavis as a bot user (chat.postMessage / chat.update, Block Kit buttons, reactions, files).

It implements the same Channel protocol as Telegram. The protocol's `chat_id` is an opaque handle: for Slack
it is the string `slack:<team>:<channel>[:<thread_ts>]` (see `chat_id` / `parse_chat`), and message ids are
the message `ts` encoded as an integer (`ts_to_id` / `id_to_ts`: "1760000100.000100" <-> 1760000100000100),
so cards and edits work unchanged. The bot token for a team comes from a resolver (the sealed native grant).
Nothing here logs a token or a response body.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

from mavis.channels.base import ChannelRateLimited, MessageGone
from mavis.channels.slack_format import (
    SECTION_LIMIT,
    message_blocks,
    plain_fallback,
    to_mrkdwn,
)
from mavis.channels.text import split_text
from mavis.domain.messages import Button

log = structlog.get_logger(__name__)

API = "https://slack.com/api/"
CHAT_PREFIX = "slack:"
_GONE = frozenset({"message_not_found", "cant_update_message", "edit_window_closed", "cant_delete_message"})

# Mood reactions (Telegram's unicode set) to Slack emoji names. Anything not here is skipped, never an error.
EMOJI_NAMES: dict[str, str] = {
    "\U0001f525": "fire", "\U0001f3c6": "trophy", "\U0001f389": "tada", "\U0001f44f": "clap",
    "\U0001f4af": "100", "\U0001f44d": "+1", "⚡": "zap", "\U0001f929": "star-struck",
    "❤️‍\U0001f525": "heart_on_fire", "\U0001f601": "grin", "\U0001f923": "rolling_on_the_floor_laughing",
    "\U0001f91d": "handshake", "\U0001f64f": "pray", "\U0001fae1": "saluting_face", "❤": "heart",
    "\U0001f917": "hugging_face", "\U0001f914": "thinking_face", "\U0001f913": "nerd_face",
    "\U0001f92f": "exploding_head", "\U0001f60e": "sunglasses", "✍": "writing_hand",
    "\U0001f44c": "ok_hand", "\U0001f440": "eyes", "\U0001f634": "sleeping",
}
_VS16 = "️"


def emoji_name(emoji: str) -> str | None:
    return EMOJI_NAMES.get(emoji) or EMOJI_NAMES.get(emoji.replace(_VS16, ""))


@dataclass(frozen=True)
class SlackChat:
    team: str
    channel: str
    thread_ts: str | None = None


def chat_id(team: str, channel: str, thread_ts: str | None = None) -> str:
    return f"{CHAT_PREFIX}{team}:{channel}" + (f":{thread_ts}" if thread_ts else "")


def is_slack_chat(chat: object) -> bool:
    return isinstance(chat, str) and chat.startswith(CHAT_PREFIX)


def parse_chat(chat: str | int) -> SlackChat:
    if not is_slack_chat(chat):
        raise ValueError("not a Slack chat handle")
    parts = str(chat)[len(CHAT_PREFIX):].split(":")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("malformed Slack chat handle")
    return SlackChat(parts[0], parts[1], parts[2] if len(parts) > 2 and parts[2] else None)


def in_dm(chat: str | int) -> bool:
    """A direct message channel (ids start with D): private to one person."""
    return parse_chat(chat).channel.startswith("D")


def dm_of(chat: str | int) -> str:
    """The same workspace's handle with the thread dropped (the channel itself)."""
    c = parse_chat(chat)
    return chat_id(c.team, c.channel)


def ts_to_id(ts: str) -> int:
    whole, _, frac = str(ts).partition(".")
    return int(whole) * 1_000_000 + int((frac + "000000")[:6])


def id_to_ts(n: int) -> str:
    whole, frac = divmod(int(n), 1_000_000)
    return f"{whole}.{frac:06d}"


class SlackError(RuntimeError):
    """A Slack API refusal. The message is the stable error code only, never a body or a token."""

    def __init__(self, code: str) -> None:
        super().__init__(f"slack: {code}")
        self.code = code


TokenResolver = Callable[[str], Awaitable[str | None]]


class SlackChannel:
    def __init__(self, token_for_team: TokenResolver, client: httpx.AsyncClient | None = None,
                 presence_emoji: Callable[[], str] | None = None) -> None:
        self._token_for_team = token_for_team
        self._http = client or httpx.AsyncClient(timeout=15)
        self._presence_emoji = presence_emoji or (lambda: "")
        self._reacted: OrderedDict[tuple[str, str], str] = OrderedDict()  # (channel, ts) -> our reaction name
        self._dms: dict[tuple[str, str], str] = {}

    # --- transport -----------------------------------------------------------------------------------

    async def _call(self, team: str, method: str, body: dict[str, Any], *,
                    form: bool = False) -> dict[str, Any]:
        """One Web API call. The files.* methods take form encoding only (JSON values stringified)."""
        token = await self._token_for_team(team)
        if not token:
            raise SlackError("bot_not_installed")
        try:
            headers = {"Authorization": f"Bearer {token}"}
            if form:
                fields = {k: v if isinstance(v, str) else json.dumps(v) for k, v in body.items()}
                r = await self._http.post(API + method, data=fields, headers=headers)
            else:
                r = await self._http.post(API + method, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise SlackError(f"transport_{type(exc).__name__}") from None
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("retry-after", "1"))
            except ValueError:
                wait = 1.0
            raise ChannelRateLimited(max(wait, 0.5))
        try:
            data = r.json()
        except ValueError:
            raise SlackError(f"http_{r.status_code}") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            code = str(data.get("error", "unknown")) if isinstance(data, dict) else "unknown"
            if code == "ratelimited":
                raise ChannelRateLimited(1.0)
            raise SlackError(code)
        return data

    # --- Channel protocol ----------------------------------------------------------------------------

    async def send_text(self, chat_id: Any, text: str,
                        buttons: list[list[Button]] | None = None) -> list[int]:
        c = parse_chat(chat_id)
        chunks = [to_mrkdwn(p) for p in split_text(text, SECTION_LIMIT)]
        chunks = [p for p in chunks if p.strip()]
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            body: dict[str, Any] = {"channel": c.channel, "text": chunk, "unfurl_links": False,
                                    "unfurl_media": False}
            if c.thread_ts:
                body["thread_ts"] = c.thread_ts
            if last and buttons:
                body["blocks"] = message_blocks([chunk], buttons)
                body["text"] = plain_fallback(text)
            data = await self._call(c.team, "chat.postMessage", body)
            ids.append(ts_to_id(str(data.get("ts") or "0")))
        return ids

    async def edit_text(self, chat_id: Any, message_id: int, text: str,
                        buttons: list[list[Button]] | None = None) -> None:
        c = parse_chat(chat_id)
        chunk = to_mrkdwn(text[:SECTION_LIMIT])
        body: dict[str, Any] = {"channel": c.channel, "ts": id_to_ts(message_id), "text": chunk}
        if buttons:
            body["blocks"] = message_blocks([chunk], buttons)
            body["text"] = plain_fallback(text)
        else:
            body["blocks"] = []  # omitting blocks would keep the old buttons
        try:
            await self._call(c.team, "chat.update", body)
        except SlackError as exc:
            if exc.code in _GONE:
                raise MessageGone(exc.code) from None
            raise

    async def send_typing(self, chat_id: Any) -> None:
        return None  # bots have no typing indicator in a plain DM

    async def react(self, chat_id: Any, message_id: int, emoji: str | None) -> None:
        c = parse_chat(chat_id)
        ts = id_to_ts(message_id)
        key = (c.channel, ts)
        name = emoji_name(emoji) if emoji else None
        if emoji and name is None:
            return  # no Slack equivalent: skip quietly
        seen = self._presence_emoji()
        previous = self._reacted.get(key) or (emoji_name(seen) if seen else None)
        if previous and previous != name:
            try:
                await self._call(c.team, "reactions.remove", {"channel": c.channel, "timestamp": ts,
                                                              "name": previous})
            except SlackError as exc:
                if exc.code != "no_reaction":
                    raise
            self._reacted.pop(key, None)
        if name:
            try:
                await self._call(c.team, "reactions.add", {"channel": c.channel, "timestamp": ts,
                                                           "name": name})
            except SlackError as exc:
                if exc.code != "already_reacted":
                    raise
            self._reacted[key] = name
            while len(self._reacted) > 500:
                self._reacted.popitem(last=False)

    async def download_file(self, file_id: str, dest_path: str) -> str:
        raise NotImplementedError("Slack file downloads are not supported")

    async def send_document(self, chat_id: Any, path: str, caption: str = "") -> int:
        c = parse_chat(chat_id)
        file = Path(path)
        content = file.read_bytes()  # noqa: ASYNC240 - a small local file
        slot = await self._call(c.team, "files.getUploadURLExternal",
                                {"filename": file.name, "length": len(content)}, form=True)
        try:
            up = await self._http.post(str(slot["upload_url"]), content=content)
        except httpx.HTTPError as exc:
            raise SlackError(f"transport_{type(exc).__name__}") from None
        if up.status_code >= 300:
            raise SlackError(f"upload_http_{up.status_code}")
        body: dict[str, Any] = {"files": [{"id": slot["file_id"], "title": file.name}],
                                "channel_id": c.channel}
        if caption:
            body["initial_comment"] = to_mrkdwn(caption)[:SECTION_LIMIT]
        if c.thread_ts:
            body["thread_ts"] = c.thread_ts
        await self._call(c.team, "files.completeUploadExternal", body, form=True)
        return 0

    async def send_photo(self, chat_id: Any, path: str, caption: str = "") -> int:
        return await self.send_document(chat_id, path, caption)

    async def send_media_group(self, chat_id: Any, paths: list[str],
                               captions: list[str] | None = None) -> list[int]:
        caps = list(captions or [""] * len(paths))
        return [await self.send_document(chat_id, p, caps[i] if i < len(caps) else "")
                for i, p in enumerate(paths[:10])]

    # --- Slack only ----------------------------------------------------------------------------------

    async def open_dm(self, team: str, slack_user: str) -> str:
        """The DM channel between the bot and `slack_user` (conversations.open, cached)."""
        key = (team, slack_user)
        if key not in self._dms:
            data = await self._call(team, "conversations.open", {"users": slack_user})
            channel = str((data.get("channel") or {}).get("id") or "")
            if not channel:
                raise SlackError("no_dm_channel")
            self._dms[key] = channel
        return self._dms[key]
