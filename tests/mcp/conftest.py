"""Fixtures for the MCP tests."""

from __future__ import annotations

import base64
import os

import pytest

from mavis.config import get_settings


@pytest.fixture
def mcp_env(settings, monkeypatch):
    """The feature on, a token KEK for sealing, a public base URL, and the dev-only localhost allowance (the
    fake servers live on 127.0.0.1)."""
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_ALLOW_LOCALHOST", "true")
    monkeypatch.setenv("NATIVE_TOKEN_KEK", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mavis.test")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def two_users(db):
    from mavis.store.repo import users

    a, _ = await users.get_or_create_by_chat(111, "Ann")
    b, _ = await users.get_or_create_by_chat(222, "Bob")
    return a, b
