from __future__ import annotations

import base64
import os
from collections.abc import Callable

import httpx
import pytest

from mavis.config import get_settings
from mavis.tools.integrations.native.oauth import NativeOAuth
from mavis.tools.integrations.native.tokens import NativeTokenStore


def new_kek() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def native_env(settings, monkeypatch):
    for key, value in {
        "NATIVE_TOKEN_KEK": new_kek(), "GOOGLE_OAUTH_CLIENT_ID": "gid",
        "GOOGLE_OAUTH_CLIENT_SECRET": "gsecret",
        "SLACK_CLIENT_ID": "sid", "SLACK_CLIENT_SECRET": "ssecret", "PUBLIC_BASE_URL": "https://mavis.test",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


class Vendor:
    """A scripted vendor: records every request, answers with the handler (url -> Response)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        handler = self.routes.get(url)
        if handler is None:
            return httpx.Response(404, json={"error": "unrouted"})
        return handler(request)

    def to(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if f"{r.url.scheme}://{r.url.host}{r.url.path}" == url]


def form(request: httpx.Request) -> dict[str, str]:
    from urllib.parse import parse_qsl

    return dict(parse_qsl(request.content.decode()))


@pytest.fixture
def vendor() -> Vendor:
    return Vendor()


@pytest.fixture
async def client(vendor):
    async with httpx.AsyncClient(transport=httpx.MockTransport(vendor)) as c:
        yield c


async def seed_users(count: int = 40, status: str = "active") -> None:
    """Mavis users 1..count. Connecting an account now needs a user the access rules admit, as in prod."""
    from mavis.store import db as dbm
    from mavis.store.models import User

    async with dbm.Session() as s:
        s.add_all(User(id=i, timezone="UTC", state={}, status=status) for i in range(1, count + 1))
        await s.commit()


@pytest.fixture
async def tokens(native_env, db, client):
    await seed_users()
    return NativeTokenStore(client)


@pytest.fixture
def oauth(tokens, client):
    return NativeOAuth(tokens, client)
