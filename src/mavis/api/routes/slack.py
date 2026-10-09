"""Slack Events API endpoint. Verify the signature over the raw body, then parse. Never calls an LLM."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from mavis.bus import get_bus
from mavis.bus.base import EventBus
from mavis.config import get_settings
from mavis.domain.errors import IntegrationError, WebhookVerificationError
from mavis.tools.integrations.native import slack_events

router = APIRouter()


async def _read_capped(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise HTTPException(status_code=413, detail="body too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise HTTPException(status_code=413, detail="body too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhooks/slack")
async def slack_webhook(request: Request, bus: EventBus = Depends(get_bus)) -> dict[str, Any]:
    body = await _read_capped(request, slack_events.MAX_BODY_BYTES)  # never logged
    secret = getattr(get_settings(), "slack_signing_secret", "")  # added by the settings branch
    try:
        return await slack_events.handle_request(
            secret, dict(request.headers), body, slack_events.get_user_lookup(), bus
        )
    except WebhookVerificationError:
        raise HTTPException(status_code=401, detail="invalid signature") from None
    except IntegrationError:
        raise HTTPException(status_code=400, detail="unrecognised payload") from None


@router.post("/webhooks/slack/interactive")
async def slack_interactive(request: Request, bus: EventBus = Depends(get_bus)) -> dict[str, Any]:
    """Block Kit button presses and slash commands (signed form posts). Acknowledged within Slack's 3 seconds:
    the work is a bus publish and, at most, one response_url call."""
    from mavis.channels import slack_interactive as interactive

    body = await _read_capped(request, slack_events.MAX_BODY_BYTES)
    secret = getattr(get_settings(), "slack_signing_secret", "")
    try:
        return await interactive.handle_request(
            secret, dict(request.headers), body, slack_events.get_user_lookup(), bus
        )
    except WebhookVerificationError:
        raise HTTPException(status_code=401, detail="invalid signature") from None
    except IntegrationError:
        raise HTTPException(status_code=400, detail="unrecognised payload") from None
