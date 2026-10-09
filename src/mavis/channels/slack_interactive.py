"""Slack interactivity: Block Kit button presses (block_actions) and slash commands.

Both arrive as a form POST signed like every Slack request (v0 HMAC over the raw body); the signature is
verified before anything is parsed. A press becomes the same BUTTON_PRESSED event a Telegram tap makes (the
callback data is the button's `value`), so approvals, task cancel and the rest need no Slack-specific code.
A press is accepted only from the Slack user who owns the card: the presser must map to a Mavis user, and the
card must sit in that user's own DM with the bot. Anyone else gets a private (ephemeral) refusal and nothing
is published. The pressed card is then updated through its response_url so it cannot be pressed twice.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import structlog

from mavis.bus.base import EventBus
from mavis.channels import routing
from mavis.channels.slack import parse_chat, ts_to_id
from mavis.domain.errors import IntegrationError
from mavis.domain.events import Event, EventType, Trust
from mavis.tools.integrations.native import slack_events

log = structlog.get_logger(__name__)

RESPONSE_URL_PREFIX = "https://hooks.slack.com/"
SLASH_COMMANDS = frozenset({"channel", "connect", "connections", "disconnect"})
KEEP_OPEN_SUFFIXES = (":edit",)  # pressing Edit does not close the card
NOT_YOURS = "This card isn't yours, so I left it alone."
UNKNOWN = ("I don't know you yet. Mavis is by invite: join on Telegram with your invite link, then send "
           "/connect slack there.")
NO_DM = "I can't reach your DM with me yet. Message me there once, then try again."

Poster = Callable[[str, dict], Awaitable[None]]


async def _http_post(url: str, body: dict) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(url, json=body)


_poster: Poster = _http_post


def set_poster(poster: Poster | None) -> None:
    global _poster
    _poster = poster or _http_post


async def _respond(url: str, body: dict) -> None:
    """Post to a response_url (only Slack's own host). Best effort: a failed update never fails the press."""
    if not str(url).startswith(RESPONSE_URL_PREFIX):
        return
    try:
        await _poster(url, body)
    except Exception as exc:  # noqa: BLE001
        log.warning("slack.response_url_failed", error=type(exc).__name__)


def _ephemeral(text: str) -> dict:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}


def _answered_blocks(message: dict, action: dict) -> list[dict] | None:
    """The card's blocks without the row that was pressed, plus a note of the choice."""
    blocks = message.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    pressed = str(action.get("block_id") or "")
    kept = [b for b in blocks if isinstance(b, dict) and not (
        b.get("type") == "actions" and (not pressed or b.get("block_id") == pressed))]
    label = str(((action.get("text") or {}).get("text")) or "").strip()
    if label:
        kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Selected: {label}"}]})
    return kept


async def _publish(event: Event, bus: EventBus) -> bool:
    return await bus.publish(event)


async def _owner_chat(lookup: Any, team: str, slack_user: str,
                      channel: str | None) -> tuple[int | None, str | None, str]:
    """(Mavis user, their DM chat, refusal text). The refusal is empty when the presser owns this channel."""
    user_id = await lookup.user_for_slack(team, slack_user) if lookup is not None else None
    if user_id is None:
        return None, None, UNKNOWN
    dm = await routing.slack_chat_for(user_id)
    if dm is None:
        return user_id, None, NO_DM
    if channel is not None and parse_chat(dm).channel != channel:
        return user_id, dm, NOT_YOURS
    return user_id, dm, ""


async def _block_actions(payload: dict, lookup: Any, bus: EventBus) -> dict[str, Any]:
    actions = [a for a in payload.get("actions") or [] if isinstance(a, dict)]
    action = actions[0] if actions else {}
    data = str(action.get("value") or "")
    response_url = str(payload.get("response_url") or "")
    if not data or str(action.get("action_id") or "").startswith("url:"):
        return {"ignored": "no callback"}
    team = str((payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or "")
    slack_user = str((payload.get("user") or {}).get("id") or "")
    container = payload.get("container") or {}
    message = payload.get("message") or {}
    channel = str((payload.get("channel") or {}).get("id") or container.get("channel_id") or "")
    msg_ts = str(container.get("message_ts") or message.get("ts") or "")
    user_id, dm, refusal = await _owner_chat(lookup, team, slack_user, channel)
    if refusal or user_id is None or dm is None or not msg_ts:
        await _respond(response_url, _ephemeral(refusal or NOT_YOURS))
        return {"refused": 1}
    trigger = str(payload.get("trigger_id") or action.get("action_ts") or "")
    event = Event(id=f"slack:btn:{team}:{channel}:{msg_ts}:{trigger}", user_id=user_id,
                  type=EventType.BUTTON_PRESSED, occurred_at=datetime.now(UTC), source=routing.SLACK_SOURCE,
                  payload={"data": data, "message_id": ts_to_id(msg_ts), "reply_chat": dm},
                  trust=Trust.USER)
    published = await _publish(event, bus)
    if published and not data.endswith(KEEP_OPEN_SUFFIXES):
        blocks = _answered_blocks(message, action)
        if blocks is not None:
            await _respond(response_url, {"replace_original": True, "text": str(message.get("text") or " "),
                                          "blocks": blocks})
    return {"published": int(published), "duplicate": int(not published)}


async def _slash(form: dict[str, str], lookup: Any, bus: EventBus) -> dict[str, Any]:
    name = form.get("command", "").lstrip("/").lower()
    if name not in SLASH_COMMANDS:
        return _ephemeral("I don't know that command.")
    team, slack_user = form.get("team_id", ""), form.get("user_id", "")
    user_id, dm, refusal = await _owner_chat(lookup, team, slack_user, None)
    if refusal or user_id is None or dm is None:
        return _ephemeral(refusal)
    text = f"/{name} {form.get('text', '')}".strip()
    event = Event(id=f"slack:cmd:{team}:{form.get('trigger_id') or form.get('command', '')}", user_id=user_id,
                  type=EventType.USER_MESSAGE, occurred_at=datetime.now(UTC), source=routing.SLACK_SOURCE,
                  payload={"text": text, "command": name, "reply_chat": dm}, trust=Trust.USER)
    await _publish(event, bus)
    return _ephemeral("Okay, I'll answer in our DM.")


async def handle_request(secret: str, headers: Mapping[str, str], body: bytes, lookup: Any, bus: EventBus,
                         *, now: float | None = None) -> dict[str, Any]:
    """Verify, then parse. Raises WebhookVerificationError on a bad signature, IntegrationError on junk."""
    if len(body) > slack_events.MAX_BODY_BYTES:
        raise IntegrationError("Slack body too large")
    slack_events.verify_signature(secret, headers, body, now=now)
    try:
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
    except UnicodeDecodeError:
        raise IntegrationError("Slack body is not form data") from None
    if "payload" in form:
        try:
            payload = json.loads(form["payload"])
        except ValueError:
            raise IntegrationError("Slack payload is not JSON") from None
        if not isinstance(payload, dict):
            raise IntegrationError("Slack payload is not an object")
        if payload.get("type") == "block_actions":
            return await _block_actions(payload, lookup, bus)
        return {"ignored": str(payload.get("type") or "")}
    if form.get("command"):
        return await _slash(form, lookup, bus)
    raise IntegrationError("unrecognised Slack interaction")
