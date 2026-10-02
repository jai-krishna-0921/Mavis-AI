"""Shared fixtures for every phase. Later tasks append fixtures to this file."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

TEST_ENV = {
    "ENV": "test",
    "REDIS_URL": "",
    "QDRANT_URL": "",
    "NEO4J_URI": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_WEBHOOK_SECRET": "",
    "TELEGRAM_MODE": "polling",
    "ALLOWED_TELEGRAM_CHAT_IDS": "[]",
    "OLLAMA_API_KEY": "test-key",
    "COMPOSIO_API_KEY": "",
    "TAVILY_API_KEY": "",
    "LANGFUSE_PUBLIC_KEY": "",
    "LANGFUSE_SECRET_KEY": "",
    "DEMO_TIME_SCALE": "1.0",
}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Iterator:
    """Isolated Settings: temp data dir, temp SQLite, no external services, no developer .env."""
    from zento.config import get_settings

    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "data" / "artifacts"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.chdir(tmp_path)  # Settings reads ".env" relative to cwd; the temp dir has none
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def db(settings):
    """Fresh SQLite schema per test (create_all). Disposes the engine afterwards."""
    from zento.store.db import dispose_engine, init_db

    await dispose_engine()
    await init_db()
    yield
    await dispose_engine()


@pytest.fixture
def fake_llm(monkeypatch):
    """Replaces zento.llm.models.chat_model and .structured with a scripted FakeLLM."""
    from tests.fakes.llm import FakeLLM
    from zento.llm import models

    fake = FakeLLM()
    monkeypatch.setattr(models, "chat_model", fake.chat_model)
    monkeypatch.setattr(models, "structured", fake.structured)
    return fake

