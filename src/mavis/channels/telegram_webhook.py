"""Telegram Bot API webhook management (setWebhook / deleteWebhook / getWebhookInfo).

Webhook and getUpdates long-polling are mutually exclusive: while a webhook is set, a running
`mavis dev` poller gets 409 Conflict. Stop the poller (or delete the webhook) before switching.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from mavis.config import get_settings

WEBHOOK_PATH = "/telegram/webhook"
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]  # same as the poller


async def _call(method: str, payload: dict[str, Any] | None = None) -> Any:
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {})
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Telegram {method} failed: HTTP {r.status_code}") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description', r.status_code)}")
    return data["result"]


async def set_webhook() -> Any:
    s = get_settings()
    base = s.public_base_url.rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError(f"PUBLIC_BASE_URL must be https for webhooks, got {base!r}")
    if not s.telegram_webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is not set")
    return await _call("setWebhook", {
        "url": f"{base}{WEBHOOK_PATH}",
        "secret_token": s.telegram_webhook_secret,
        "allowed_updates": ALLOWED_UPDATES,
        "drop_pending_updates": False,
    })


# The "/" menu every user sees (owner-only commands stay unlisted). Each entry must be a command the bot
# handles; tests/channels/test_bot_commands.py checks that against the handlers.
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Start or restart Mavis"),
    ("settings", "Your preferences"),
    ("clear", "Clear this chat or start fresh"),
    ("connect", "Connect Google or Slack"),
    ("connections", "See connected accounts"),
    ("disconnect", "Disconnect an account"),
    ("mute", "Stop learning from a sender or channel"),
    ("unmute", "Learn from a sender or channel again"),
    ("channel", "Where Mavis messages you: Telegram, Slack or both"),
    ("delete_me", "Delete your account and data"),
)


async def set_commands() -> Any:
    """Publish BOT_COMMANDS as the bot's command menu (idempotent; Telegram replaces the list)."""
    return await _call("setMyCommands", {
        "commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS],
    })


# The bot's public profile: the name in chats, the short line on its profile and in shares (max 120), and
# the "What can this bot do?" text a new user sees above Start (max 512). Plain text, no dashes.
BOT_NAME = "Mavis AI"
BOT_SHORT_DESCRIPTION = (
    "Your personal assistant: email, calendar, Drive, to-dos and polished docs, all in one chat. Invite only."
)
BOT_DESCRIPTION = (
    "Mavis is your personal assistant. Connect your Google account and it learns how you work.\n\n"
    "• Reads your inbox and drafts replies\n"
    "• Plans your calendar and keeps your to-dos\n"
    "• Finds files in Drive and answers from them\n"
    "• Makes decks, docs and sheets in your style\n"
    "• Researches the web and reports back\n"
    "• Reminds you before things slip\n\n"
    "Nothing goes out to anyone without your OK. Mavis is invite only for now: open your invite link, "
    "then tap Start."
)
AVATAR = Path(__file__).parent / "brand" / "mavis-avatar.jpg"  # the mark on the brand orange, 640 px


async def set_profile() -> dict[str, bool]:
    """Publish the name, descriptions and command menu; each is sent only when Telegram's copy differs
    (setMyName and friends are rate limited, and this runs on every api start). Returns what changed."""
    changed: dict[str, bool] = {}
    for getter, setter, key, value in (
        ("getMyName", "setMyName", "name", BOT_NAME),
        ("getMyShortDescription", "setMyShortDescription", "short_description", BOT_SHORT_DESCRIPTION),
        ("getMyDescription", "setMyDescription", "description", BOT_DESCRIPTION),
    ):
        current = (await _call(getter)).get(key, "")
        changed[key] = current != value
        if changed[key]:
            await _call(setter, {key: value})
    commands = [{"command": c, "description": d} for c, d in BOT_COMMANDS]
    changed["commands"] = await _call("getMyCommands") != commands
    if changed["commands"]:
        await set_commands()
    return changed


async def set_photo(path: Path = AVATAR) -> Any:
    """Upload the profile photo (a JPG). Each upload is a new photo, so this runs on request only
    (`mavis telegram set-photo`), never at start."""
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    photo = await asyncio.to_thread(path.read_bytes)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{token}/setMyProfilePhoto",
            data={"photo": json.dumps({"type": "static", "photo": "attach://avatar"})},
            files={"avatar": (path.name, photo, "image/jpeg")},
        )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram setMyProfilePhoto failed: {data.get('description', r.status_code)}")
    return data["result"]


async def delete_webhook() -> Any:
    return await _call("deleteWebhook", {"drop_pending_updates": False})


async def webhook_info() -> Any:
    info = await _call("getWebhookInfo")
    # the url embeds no secret, but never echo anything token-like back
    return {k: v for k, v in info.items() if k != "secret_token"}
