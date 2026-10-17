# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Fathom webhook: one signed URL per user. Verify the signature over the raw body, drop replays, queue a
FATHOM_RECORDING job. Never calls an LLM; the route token only finds the user, the payload never routes."""

from __future__ import annotations

import json
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from mavis.api.routes.slack import _read_capped
from mavis.bus import get_bus
from mavis.bus.base import EventBus
from mavis.domain.events import Job, JobKind
from mavis.tools.integrations.native.fathom_api import verify_fathom_signature
from mavis.tools.integrations.native.fathom_spec import hook_owner, hook_secret
from mavis.worker import locks

log = structlog.get_logger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 1024 * 1024
REPLAY_TTL_S = 86400


def _tokens():
    from mavis.tools.integrations import get_provider

    return getattr(get_provider(), "tokens", None)


@router.post("/webhooks/fathom/{route_token}", status_code=204)
async def fathom_webhook(route_token: str, request: Request, bus: EventBus = Depends(get_bus)) -> Response:
    body = await _read_capped(request, MAX_BODY_BYTES)  # never logged
    tokens = _tokens()
    user_id = await hook_owner(tokens, route_token) if tokens is not None else None
    if user_id is None:
        raise HTTPException(status_code=404, detail="not found")
    secret = await hook_secret(tokens, user_id)
    if not secret or not verify_fathom_signature(secret, request.headers, body, time.time()):
        raise HTTPException(status_code=401, detail="invalid signature")
    webhook_id = request.headers.get("webhook-id", "")
    if not await locks.claim(f"fathom_hook:{webhook_id}", REPLAY_TTL_S):
        return Response(status_code=204)  # a replay of a delivery already taken
    try:
        meeting = json.loads(body)
    except ValueError:
        meeting = None
    recording_id = meeting.get("recording_id") if isinstance(meeting, dict) else None
    if isinstance(recording_id, bool) or not isinstance(recording_id, int) or recording_id < 1:
        raise HTTPException(status_code=400, detail="unrecognised payload")
    try:
        await bus.enqueue(Job(id=f"fathom:{user_id}:{recording_id}", user_id=user_id,
                              kind=JobKind.FATHOM_RECORDING,
                              payload={"meeting": meeting, "source": "webhook"}))
    except Exception:
        await locks.release(f"fathom_hook:{webhook_id}")  # let Fathom's retry of this delivery through
        raise
    log.info("fathom.webhook_queued", user_id=user_id, recording_id=recording_id)
    return Response(status_code=204)
