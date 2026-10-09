"""Who is signed in, and their preferences."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mavis.access import preferences, web_prefs
from mavis.api.dashboard import common
from mavis.api.dashboard.common import DashError
from mavis.api.dashboard.connectors import native_provider
from mavis.api.dashboard.invites import left_for
from mavis.channels import routing
from mavis.channels.telegram_links import telegram_link
from mavis.config import get_settings
from mavis.store.repo import users
from mavis.tools.integrations.native.base import NativeProvider
from mavis.web import emails
from mavis.web.sessions import Active

router = APIRouter()


async def _slack_channel(user_id: int, provider: Any) -> dict:
    tokens = getattr(provider, "tokens", None)
    account = await tokens.account(user_id, NativeProvider.SLACK) if tokens is not None else None
    if not account:
        return {"connected": False, "open_url": "https://slack.com/app_redirect"}
    team = account.get("team_id") or ""
    url = f"https://slack.com/app_redirect?team={team}" if team else "https://slack.com/app_redirect"
    return {"connected": True, "label": account.get("team_name") or None, "open_url": url}


@router.get("/me")
async def me(active: Active = Depends(common.authed), provider: Any = Depends(native_provider)) -> dict:
    user = active.user
    bot = get_settings().telegram_bot_username
    return {
        "user_id": str(user.id), "name": user.name or "", "timezone": user.timezone,
        "currency": user.currency or "", "email": await emails.primary(user.id), "csrf": active.csrf,
        "invites_left": await left_for(user),
        "channels": {
            "telegram": {"connected": user.telegram_chat_id is not None,
                         "open_url": telegram_link(bot) if bot else None},
            "slack": await _slack_channel(user.id, provider),
        },
    }


def _prefs(user, state: dict) -> dict:
    start, end = web_prefs.quiet_hours(user)
    return {"name": user.name or "", "timezone": user.timezone,
            "quiet_hours": {"start": f"{start:02d}:00", "end": f"{end:02d}:00"},
            "proactive_channel": routing.pref_of(state), "morning_checkin_time": web_prefs.morning_time(user),
            "language_register_opt_out": web_prefs.register_opt_out(user)}


@router.get("/preferences")
async def get_preferences(active: Active = Depends(common.authed)) -> dict:
    user = await users.get(active.user.id)
    return _prefs(user, user.state or {})


class PrefsBody(BaseModel):
    model_config = {"extra": "ignore"}
    name: str | None = None
    timezone: str | None = None
    quiet_hours: dict | None = None
    proactive_channel: str | None = None
    morning_checkin_time: str | None = None
    language_register_opt_out: bool | None = None


@router.patch("/preferences")
async def patch_preferences(body: PrefsBody, active: Active = Depends(common.authed)) -> dict:
    uid = active.user.id
    sent = body.model_fields_set
    try:
        if "name" in sent and body.name is not None:
            await preferences.set_name(uid, body.name)
        if "timezone" in sent and body.timezone is not None:
            await preferences.set_timezone(uid, body.timezone)
        if "proactive_channel" in sent and body.proactive_channel is not None:
            choice = body.proactive_channel
            if choice not in routing.PREFS:
                raise DashError(400, "invalid_value", "Choose Telegram, Slack or both.")
            if choice != "telegram" and await routing.slack_chat_for(uid) is None:
                raise DashError(400, "slack_not_ready",
                                "Connect Slack and approve the bot first, then try again.")
            await routing.set_pref(uid, choice)
        fields = ("quiet_hours", "morning_checkin_time", "language_register_opt_out")
        extra = {k: getattr(body, k) for k in fields if k in sent}
        await web_prefs.update(uid, extra)
    except (ValueError, web_prefs.PrefError) as exc:
        raise DashError(400, "invalid_value", str(exc) if isinstance(exc, web_prefs.PrefError)
                        else "That value is not valid.") from None
    user = await users.get(uid)
    return _prefs(user, user.state or {})
