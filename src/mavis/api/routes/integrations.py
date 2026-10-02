"""Inbound provider events (push). Verify, normalise, publish. Never calls an LLM."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from mavis.bus import get_bus
from mavis.bus.base import EventBus
from mavis.domain.errors import IntegrationError, WebhookVerificationError
from mavis.tools.integrations import get_provider
from mavis.tools.integrations.base import IntegrationProvider

router = APIRouter()


@router.post("/webhooks/integrations")
async def integrations_webhook(
    request: Request,
    bus: EventBus = Depends(get_bus),
    provider: IntegrationProvider = Depends(get_provider),
) -> dict[str, int]:
    body = await request.body()  # never logged
    try:
        events = provider.parse_webhook(dict(request.headers), body)
    except WebhookVerificationError:
        raise HTTPException(status_code=401, detail="invalid signature") from None
    except IntegrationError:
        raise HTTPException(status_code=400, detail="unrecognised payload") from None
    accepted = 0
    for event in events:
        if await bus.publish(event):
            accepted += 1
    return {"accepted": accepted, "received": len(events)}
