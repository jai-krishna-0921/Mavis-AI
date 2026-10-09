"""Short-lived signed values (Google sign-in cookie, the link-telegram token). HMAC-SHA256 over a JSON body
with an expiry; the key is derived from server secrets, never sent anywhere."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from mavis.config import get_settings


def _key(purpose: str) -> bytes:
    s = get_settings()
    material = f"{s.google_oauth_client_secret}|{s.native_token_kek}|{s.telegram_webhook_secret}"
    return hashlib.sha256(f"mavis-web:{purpose}:{material}".encode()).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(purpose: str, body: dict[str, Any], ttl_s: int) -> str:
    payload = _b64(json.dumps({**body, "exp": int(time.time()) + ttl_s}, separators=(",", ":")).encode())
    return f"{payload}.{_b64(hmac.new(_key(purpose), payload.encode(), hashlib.sha256).digest())}"


def verify(purpose: str, value: str | None) -> dict[str, Any] | None:
    """The body when the signature is good and it has not expired, else None."""
    if not value or value.count(".") != 1 or len(value) > 2000:
        return None
    payload, sig = value.split(".")
    good = _b64(hmac.new(_key(purpose), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(good.encode(), sig.encode()):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(body, dict) or int(body.get("exp", 0)) < time.time():
        return None
    return body
