# ruff: noqa: E501
"""Fixtures and helpers for the dashboard API tests (/api/v1)."""

from __future__ import annotations

import base64
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from mavis.api.app import create_app
from mavis.config import get_settings
from mavis.web import google_signin, sessions
from tests.tools.integrations.fakes import FakeProvider
from tests.tools.integrations.native.conftest import *  # noqa: F403 - native fixtures (tokens, oauth, vendor)

BASE = "https://mavis.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwks_body() -> dict:
    n = KEY.public_key().public_numbers()
    return {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                      "n": b64(n.n.to_bytes(256, "big")), "e": b64(n.e.to_bytes(3, "big"))}]}


def id_token(nonce: str, *, key=None, **over) -> str:
    import json

    kid = over.pop("kid", "k1")
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": "gid", "exp": now + 3600, "iat": now, "sub": "g-42",
              "nonce": nonce, "email": "me@orbit.test", "email_verified": True, **over}
    head = b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    body = b64(json.dumps(claims).encode())
    sig = (key or KEY).sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(sig)}"


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


@pytest.fixture
def dash_env(native_env, monkeypatch):
    for k, v in {"DASHBOARD_ENABLED": "true", "GOOGLE_SIGNIN_ENABLED": "true",
                 "TELEGRAM_BOT_USERNAME": "MavisTestBot"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def google(vendor, client):
    """The Google token and JWKS endpoints, answered by the scripted vendor; sign-in uses the same client."""
    vendor.routes[google_signin.JWKS_URL] = lambda r: httpx.Response(
        200, json=jwks_body(), headers={"cache-control": "public, max-age=300"})
    google_signin.set_client(client)
    yield vendor
    google_signin.set_client(None)


@pytest.fixture
def router(tokens, oauth, client, monkeypatch):
    """The native router as the process provider (so /connectors and the OAuth callback use real grants)."""
    import functools

    from mavis.tools.integrations.native.router import NativeRouter

    r = NativeRouter(FakeProvider(), tokens, oauth, [], client)
    monkeypatch.setattr("mavis.tools.integrations.get_provider", functools.cache(lambda: r))
    return r


@pytest.fixture
def app(dash_env, router, rec_bus, channel):
    from mavis.api.routes.oauth import get_provider  # the function the route depends on, not the patched one
    from mavis.bus import get_bus

    app = create_app()
    app.dependency_overrides[get_provider] = lambda: router  # the OAuth callback route
    app.dependency_overrides[get_bus] = lambda: rec_bus
    return app


def new_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


async def signed_in(app, user_id: int) -> tuple[httpx.AsyncClient, dict[str, str]]:
    """A client holding a live session for the user, and the CSRF header it must send."""
    c = new_client(app)
    token = await sessions.create(user_id, ip="203.0.113.9", user_agent="pytest")
    c.cookies.set(sessions.COOKIE, token)
    return c, {"X-Mavis-CSRF": sessions.csrf_for(token)}


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    from mavis.api import ratelimit

    ratelimit._buckets.clear()
    yield
    ratelimit._buckets.clear()
