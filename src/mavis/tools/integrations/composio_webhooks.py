"""Composio webhook verification (standard-webhooks HMAC) and V3 payload parsing. TEMPORARY provider glue."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from functools import partial

from mavis.domain.errors import IntegrationError, WebhookVerificationError
from mavis.domain.events import Event
from mavis.domain.integrations import user_from_provider_id
from mavis.tools.integrations.normalize import (
    calendar_event,
    email_event,
    notion_event,
    slack_event,
    workspace_event,
)

Builder = Callable[[int, dict, str], Event | None]
# googlesuper triggers carry one prefix for every Google service, so they are routed by full slug.
SLUG_BUILDERS: dict[str, Builder] = {
    "GOOGLESUPER_NEW_MESSAGE": email_event,
    "GOOGLESUPER_GOOGLE_CALENDAR_EVENT_CHANGE_TRIGGER": calendar_event,
    "GOOGLESUPER_FILE_SHARED_PERMISSIONS_ADDED": partial(workspace_event, kind="share"),
    "GOOGLESUPER_COMMENT_ADDED_TRIGGER": partial(workspace_event, kind="comment"),
    "GOOGLESUPER_NEW_TASK_CREATED_TRIGGER": partial(workspace_event, kind="task"),
    "GOOGLESUPER_TASK_UPDATED_TRIGGER": partial(workspace_event, kind="task"),
}
_BUILDERS: dict[str, Builder] = {
    "GMAIL": email_event,
    "GOOGLECALENDAR": calendar_event,
    "SLACK": slack_event,
    "NOTION": notion_event,
}


def verify_signature(
    secret: str, headers: Mapping[str, str], body: bytes, *, tolerance_s: int = 300, now: float | None = None
) -> None:
    if not secret:
        raise WebhookVerificationError("COMPOSIO_WEBHOOK_SECRET is not set; refusing unsigned webhooks")
    h = {k.lower(): v for k, v in headers.items()}
    wid, ts, sig = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
    if not (wid and ts and sig):
        raise WebhookVerificationError("missing webhook-id/webhook-timestamp/webhook-signature headers")
    try:
        ts_int = int(ts)
    except ValueError:
        raise WebhookVerificationError("malformed webhook timestamp") from None
    current = time.time() if now is None else now
    if tolerance_s <= 0:
        raise ValueError("tolerance_s must be positive")
    if abs(current - ts_int) > tolerance_s:
        raise WebhookVerificationError("stale webhook timestamp")
    try:
        text = body.decode()
    except UnicodeDecodeError:
        raise WebhookVerificationError("webhook body is not valid UTF-8") from None
    expected = base64.b64encode(
        hmac.new(secret.encode(), f"{wid}.{ts}.{text}".encode(), hashlib.sha256).digest()
    ).decode()
    candidates = [part.split(",", 1)[1] if "," in part else part for part in sig.split()]
    expected_b = expected.encode()
    if not any(hmac.compare_digest(expected_b, c.encode("utf-8", "replace")) for c in candidates):
        raise WebhookVerificationError("invalid webhook signature")


def parse_composio_webhook(
    headers: Mapping[str, str], body: bytes, secret: str, *, now: float | None = None
) -> list[Event]:
    verify_signature(secret, headers, body, now=now)
    try:
        payload = json.loads(body)
    except ValueError:
        raise IntegrationError("webhook body is not JSON") from None
    if not isinstance(payload, dict):
        raise IntegrationError("webhook body is not a JSON object")
    meta = payload.get("metadata") or {}
    data = payload.get("data") or payload.get("payload") or {}
    if not isinstance(meta, dict) or not isinstance(data, dict):
        raise IntegrationError("webhook metadata/data is not a JSON object")
    slug = str(meta.get("trigger_slug") or payload.get("trigger_name") or payload.get("type") or "").upper()
    user_id = user_from_provider_id(meta.get("user_id") or data.get("user_id"))
    builder = SLUG_BUILDERS.get(slug) or _BUILDERS.get(slug.split("_", 1)[0])
    if user_id is None or builder is None:
        return []
    event = builder(user_id, data, "composio")
    return [event] if event else []
