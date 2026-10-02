from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, Request

from zento.bus import get_bus
from zento.channels.telegram_updates import ingest_update
from zento.config import get_settings

router = APIRouter()


@router.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    secret = get_settings().telegram_webhook_secret
    # fail closed: without a configured secret nobody can be authenticated, in any mode
    if not secret or not hmac.compare_digest(
        (x_telegram_bot_api_secret_token or "").encode(), secret.encode()
    ):
        raise HTTPException(status_code=403, detail="bad secret token")
    published = await ingest_update(await request.json(), get_bus())
    return {"ok": True, "published": published}
