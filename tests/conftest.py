"""Shared fixtures for every phase. Later tasks append fixtures to this file."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from mavis.memory.embeddings import set_embedder
from mavis.memory.graph import SqliteGraphStore
from mavis.memory.service import MemoryService, set_memory
from mavis.memory.vector import QdrantVectorStore
from tests.memory.fakes import HashEmbedder

TEST_ENV = {
    "ENV": "dev",
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
    from mavis.config import get_settings

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
    from mavis.store.db import dispose_engine, init_db

    await dispose_engine()
    await init_db()
    yield
    await dispose_engine()


@pytest.fixture
def fake_llm(monkeypatch):
    """Replaces mavis.llm.models.chat_model and .structured with a scripted FakeLLM."""
    from mavis.llm import models
    from tests.fakes.llm import FakeLLM

    fake = FakeLLM()
    monkeypatch.setattr(models, "chat_model", fake.chat_model)
    monkeypatch.setattr(models, "structured", fake.structured)
    return fake



@pytest.fixture
def channel():
    """A FakeChannel installed as the process channel; inspect `.sent` / `.texts`."""
    from mavis.channels import set_channel
    from mavis.channels.fake import FakeChannel

    ch = FakeChannel()
    set_channel(ch)
    yield ch
    set_channel(None)


@pytest.fixture
async def bus(settings):
    """An InProcessBus installed as the process bus."""
    from mavis.bus import set_bus
    from mavis.bus.inprocess import InProcessBus

    b = InProcessBus()
    set_bus(b)
    yield b
    await b.close()
    set_bus(None)


@pytest.fixture(autouse=True)
def _reset_worker_registry():
    """Handlers registered by one test must not leak into the next."""
    yield
    from mavis.worker.runner import clear_handlers

    clear_handlers()


@pytest.fixture
async def user(db):
    from mavis.store.repo import users

    u, _ = await users.get_or_create_by_chat(111, "Jai")
    return u


@pytest.fixture
def embedder():
    e = HashEmbedder()
    set_embedder(e)
    yield e
    set_embedder(None)


@pytest.fixture
async def vector(embedder):
    store = QdrantVectorStore(embedder, location=":memory:")
    await store.init()
    yield store
    await store.close()


@pytest.fixture
async def graph(db):
    g = SqliteGraphStore()
    await g.init()
    return g


@pytest.fixture
async def memory(graph, vector, embedder):
    svc = MemoryService(graph, vector, embedder)
    set_memory(svc)
    yield svc
    set_memory(None)
