"""Public settings the web app needs before sign in: the bot link and which sign-ins exist."""

from __future__ import annotations

from fastapi import APIRouter, Response

from mavis.config import get_settings
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import configured

router = APIRouter()


@router.get("/config")
async def public_config(response: Response) -> dict:
    s = get_settings()
    bot = s.telegram_bot_username.strip().lstrip("@")
    response.headers["Cache-Control"] = "no-store"
    return {
        "bot_username": bot or None,
        "bot_url": f"https://t.me/{bot}" if bot else None,
        "slack_enabled": configured(NativeProvider.SLACK),
        "google_signin_enabled": bool(s.google_signin_enabled),
    }
