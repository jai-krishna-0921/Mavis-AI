"""Shared fixtures for every phase. Later tasks append fixtures to this file."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from mavis.bus import set_bus
from mavis.domain.events import Event, Job
from mavis.domain.memory import Extraction, RecallContext
from mavis.domain.messages import Outbound
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
    "ATTENTION_STRICT_ERRORS": "true",
    "GOOGLE_WORKSPACE_ENABLED": "false",  # spec 7: off in tests unless a test opts in (workspace_on)
    "DEMO_TIME_SCALE": "1.0",
    "PRESENCE_REACTION": "\N{EYES}",  # production default is off; presence tests opt in via this
}


@pytest.fixture(autouse=True)
def _reset_ollama_state():
    """Global 429 backoff / timeout cooldown is process-wide; it must not leak between tests."""
    from mavis.llm import models

    models._ollama.reset()
    yield
    models._ollama.reset()


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
def workspace_on(settings, monkeypatch):
    """GOOGLE_WORKSPACE_ENABLED=true for one test (Settings is lru_cached, so the cache is cleared)."""
    from mavis.config import get_settings

    monkeypatch.setenv("GOOGLE_WORKSPACE_ENABLED", "true")
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
def channel(settings):
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


@pytest.fixture(autouse=True)
def _reset_reaction_log(monkeypatch):
    """Reaction outcomes (the frequency rule's memory) must not leak between tests."""
    from mavis.agents import reactions

    monkeypatch.setattr(reactions, "_log", reactions.ReactionLog())


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


# Every test runs on the project's clock (timeutil.now), never the wall clock: a fixed start that ticks
# with real elapsed time, so durations still pass. The `clock` fixture replaces it with a hand-moved one.
# MAVIS_TEST_NOW (ISO-8601 with offset) moves the start: the suite must pass at any start, e.g.
# MAVIS_TEST_NOW=2026-10-06T23:30:00+05:30 (quiet hours) or 2026-11-01T01:30:00-04:00 (DST change).
DEFAULT_TEST_NOW = "2026-09-29T11:00:00+05:30"  # a Tuesday late morning in Asia/Kolkata


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    import os
    import time as _time

    from mavis.domain import timeutil

    start = datetime.fromisoformat(os.environ.get("MAVIS_TEST_NOW") or DEFAULT_TEST_NOW).astimezone(UTC)
    t0 = _time.monotonic()

    def ticking() -> datetime:
        return start + timedelta(seconds=_time.monotonic() - t0)

    monkeypatch.setattr(timeutil, "_clock", ticking)
    return start


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
        self.learned_trust: list = []  # trust passed to each learn(), parallel to `learned`
        self.forgotten: list[str] = []
        self.recall_result: RecallContext | None = None  # a test may script what recall returns

    def set_loops_reader(self, reader) -> None:
        self.loops_reader = reader

    async def recall(self, user_id: int, text: str) -> RecallContext:
        return self.recall_result or RecallContext(profile=self.profile)

    async def learn(self, user_id: int, text: str, source_ref: str = "", trust=None) -> Extraction:
        self.learned.append((user_id, text, source_ref))
        self.learned_trust.append(trust)
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

    # Phase 4 wiring (agents.wiring.register, register_integrations(registry)): the tool registry carries
    # policy hooks bound to the integration singletons, the "connect" interrupt is bound to the flow,
    # and the approval and task delivery wakeups are process-wide.
    from mavis.agents import interrupts
    from mavis.tools import registry as registry_mod

    registry_mod._REGISTRY = None
    interrupts.reset_defaults()
    for kind in ("system_approval_remind", "system_approval_expire", "system_task_delivery"):
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


# --- Phase 4 fixtures ------------------------------------------------------------------------------
# `fake_memory.profile = "Name: Jai. Friend: Jawahar."` gives a test recall content.


@pytest.fixture
def sent(monkeypatch) -> list[Outbound]:
    """Captures every Outbound passed to outbox.enqueue."""
    from mavis.store.repo import outbox

    captured: list[Outbound] = []

    async def _enqueue(session, msg: Outbound) -> int:
        captured.append(msg)
        return len(captured)

    monkeypatch.setattr(outbox, "enqueue", _enqueue)
    return captured


@pytest.fixture
def rec_bus() -> Iterator[RecordingBus]:
    """Installs a RecordingBus as the process bus (set_bus), so modules holding get_bus see it."""
    rb = RecordingBus()
    set_bus(rb)  # type: ignore[arg-type]
    yield rb
    set_bus(None)


@pytest.fixture
def fresh_registry(monkeypatch):
    from mavis.tools import registry as registry_mod

    reg = registry_mod.ToolRegistry()
    monkeypatch.setattr(registry_mod, "_REGISTRY", reg)
    return reg


@pytest.fixture
def memory_checkpointer(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    from mavis.agents import checkpointing

    saver = InMemorySaver()

    @asynccontextmanager
    async def _open():
        yield saver

    monkeypatch.setattr(checkpointing, "open_checkpointer", _open)
    return saver


class SendNoteArgs(BaseModel):
    text: str


@pytest.fixture
def note_tool(fresh_registry):
    """Registers an OUTWARD 'send_note' tool and returns the list of executed texts."""
    from mavis.domain.policy import RiskClass
    from mavis.tools.registry import MavisTool

    calls: list[str] = []

    async def _send(user_id: int, args: SendNoteArgs) -> str:
        calls.append(args.text)
        return f"sent: {args.text}"

    fresh_registry.register(
        MavisTool(
            name="send_note",
            description="Send a note to a friend.",
            args_model=SendNoteArgs,
            risk=RiskClass.OUTWARD,
            fn=_send,
            agents=frozenset({"conversation", "spawn"}),
            preview=lambda a: f"Send note: {a.text}",
        )
    )
    return calls


@pytest.fixture(autouse=True)
def _reset_attention():
    """Attention singletons, chat context providers and its system wakeups must not leak between tests."""
    yield
    from mavis.agents import context_hooks
    from mavis.attention import wiring as attention_wiring
    from mavis.timers import system
    for getter in attention_wiring.ATTENTION_GETTERS:
        getter.cache_clear()
    attention_wiring._private_clients.clear()  # in-memory clients only; nothing on disk to release
    context_hooks.clear_context_providers()
    for kind in attention_wiring.SYSTEM_KINDS:
        system.SYSTEM_WAKEUP_HANDLERS.pop(kind.value, None)
    system.SYSTEM_WAKEUP_HANDLERS.pop("system_workspace_poll", None)
    from mavis.attention import rhythm
    from mavis.tools.integrations import first_sync

    rhythm.clear_evening_sources()
    first_sync.EXTRA_HANDLERS.clear()
