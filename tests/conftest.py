"""Shared fixtures for every phase. Later tasks append fixtures to this file."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain.events import Event, Job
from mavis.domain.memory import Extraction, RecallContext
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


@pytest.fixture(autouse=True)
def _no_inline_retries(request, monkeypatch) -> None:
    """Existing bus tests count handler calls; inline retries (2s/5s/10s) are tested explicitly."""
    if request.node.get_closest_marker("inline_retries"):
        return
    from mavis.bus import base

    monkeypatch.setattr(base, "INLINE_RETRY_DELAYS_S", ())


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


@pytest.fixture(autouse=True)
def _reset_initiative_wiring():
    """The process Initiative binds a bus and memory; it must not leak between tests."""
    from mavis.initiative import wiring

    wiring.set_current(None)
    yield
    wiring.set_current(None)


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


class _Clock:
    def __init__(self) -> None:
        # Sunday 27 Sep 2026, 13:30 IST, the opening of the reference transcript.
        self.t = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)

    def set(self, t: datetime) -> None:
        self.t = t

    def advance(self, **kwargs: float) -> None:
        self.t += timedelta(**kwargs)


@pytest.fixture
def clock(monkeypatch):
    from mavis.domain import timeutil

    c = _Clock()
    monkeypatch.setattr(timeutil, "_clock", lambda: c.t)
    return c


class RecordingBus:
    """EventBus double: records publishes/enqueues, dedupes by event id."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.jobs: list[Job] = []
        self._seen: set[str] = set()

    async def publish(self, event: Event) -> bool:
        if event.id in self._seen:
            return False
        self._seen.add(event.id)
        self.events.append(event)
        return True

    async def enqueue(self, job: Job) -> None:
        self.jobs.append(job)

    async def consume_events(self, group, consumer, handler):  # pragma: no cover
        raise NotImplementedError

    async def consume_jobs(self, group, consumer, handler):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None

    def take(self) -> list[Event]:
        batch, self.events = self.events, []
        return batch


@pytest.fixture
def recording_bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def drain():
    async def _drain(bus: RecordingBus, handler, max_rounds: int = 20) -> int:
        handled = 0
        for _ in range(max_rounds):
            batch = bus.take()
            if not batch:
                return handled
            for event in batch:
                await handler.handle(event)
                handled += 1
        return handled

    return _drain


class FakeMemory:
    """MemoryService double shared by Phases 3+ (Phase 4 relies on learned/forgotten/forget)."""

    def __init__(self) -> None:
        self.on_extraction: list = []
        self.loops_reader = None
        self.profile = ""
        self.learned: list[tuple[int, str, str]] = []
        self.forgotten: list[str] = []

    def set_loops_reader(self, reader) -> None:
        self.loops_reader = reader

    async def recall(self, user_id: int, text: str) -> RecallContext:
        return RecallContext(profile=self.profile)

    async def learn(self, user_id: int, text: str, source_ref: str = "", trust=None) -> Extraction:
        self.learned.append((user_id, text, source_ref))
        return Extraction()

    async def forget(self, user_id: int, needle: str) -> int:
        self.forgotten.append(needle)
        return 2

    async def describe_user(self, user_id: int) -> str:
        return self.profile or "I don't know much about you yet."


@pytest.fixture
def fake_memory() -> Iterator[FakeMemory]:
    """A FakeMemory installed as the process memory service (set_memory), reset afterwards."""
    fm = FakeMemory()
    set_memory(fm)  # type: ignore[arg-type]
    yield fm
    set_memory(None)


# --- integrations fixtures (shared by tests/tools, tests/agents, tests/initiative) ---------------


@pytest.fixture(autouse=True)
def _reset_integrations():
    """Process-level integration singletons must not leak between tests."""
    yield
    try:
        from mavis.tools import integrations
    except ImportError:  # pragma: no cover
        return
    integrations.get_provider.cache_clear()
    integrations.get_connection_cache.cache_clear()
    from mavis.worker import locks

    locks._claims.clear()
    from mavis.agents import buttons
    from mavis.initiative import hooks, routines
    from mavis.tools.integrations import wiring

    for getter in wiring.WIRING_GETTERS:
        getter.cache_clear()
    for hook_list in (hooks.PREFILTERS, hooks.ENRICHERS, hooks.DECISION_POLICIES):
        hook_list.clear()
    routines.clear_brief_sources()
    routines.clear_morning_hooks()
    buttons.BUTTON_HANDLERS.clear()
    from mavis.timers import system
    from mavis.tools.integrations.connect_flow import CHECK_KIND
    from mavis.tools.integrations.poller import POLL_KIND

    # these two are bound to the (now cache-cleared) flow and poller; the next register call re-points them
    for kind in (CHECK_KIND, POLL_KIND):
        system.SYSTEM_WAKEUP_HANDLERS.pop(kind, None)


@pytest.fixture
def provider():
    from tests.tools.integrations.fakes import FakeProvider

    return FakeProvider()


@pytest.fixture
def cache(provider):
    from mavis.tools.integrations.connections import ConnectionCache

    return ConnectionCache(provider, ttl_s=60)


@pytest.fixture
def fake_bus():
    from tests.tools.integrations.fakes import FakeBus

    return FakeBus()


@pytest.fixture
def state():
    from tests.tools.integrations.fakes import FakeState

    return FakeState()


@pytest.fixture
def rec():
    from tests.tools.integrations.fakes import Recorder

    return Recorder()
