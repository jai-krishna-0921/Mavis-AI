# Mavis Phase 1 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Read `docs/superpowers/plans/2026-10-02-mavis-00-index.md` first: its Shared Contracts are binding.

**Goal:** A running Mavis skeleton: config, domain contracts, LLM layer, database + migrations, event bus, Telegram channel with a transactional outbox, a per-user-serialised worker, and a persona chat turn with working memory, all runnable via `mavis dev` / `mavis chat`.

**Architecture:** Telegram updates (webhook or long-polling) are normalised into `Event`s and published on an `EventBus` (in-process in dev, Redis Streams in prod). The worker dispatches events to registered handlers under a per-user lock. The Phase 1 `USER_MESSAGE` handler (`simple_turn.run_turn`) builds a persona prompt from the last 20 messages, calls the FAST model and writes reply bubbles to the `outbox` table. `OutboxSender` delivers them through the `Channel` port with retries.

**Tech Stack:** Python 3.13, uv, pydantic-settings, structlog, SQLAlchemy 2 async (aiosqlite / psycopg 3), Alembic, langchain-openai (Ollama Cloud), redis-py asyncio, python-telegram-bot 21+, FastAPI, typer, pytest + pytest-asyncio + fakeredis.

**Spec:** `docs/superpowers/specs/2026-10-02-mavis-pa-design.md` (sections 2, 3, 5.1, 8.5, 9, 11, 13)

## Contract additions

These are minimal additions to the index's contracts. Later phases may rely on them.

1. `mavis.llm.models.complete(messages, tier=Tier.FAST, temperature=0.6, name="complete") -> str`: plain-text completion, raises `LLMError`. It calls `chat_model()` through the module global, so patching `chat_model` also fakes `complete`.
2. `mavis.channels.base.ChannelRateLimited(retry_after: float)`: raised by channels on provider rate limits. Also `mavis.channels.get_channel()` / `set_channel()`.
3. `mavis.bus.set_bus()` and `mavis.bus.get_redis() -> Redis | None` (a shared client for locks and health checks). `InProcessBus.wait_idle()`, `.dead_events`, `.dead_jobs`.
4. `messages.log(user_id, role, content, proactive=False, event_id=None) -> bool` gains `event_id` (unique) so retried handlers don't double-log. `Message.event_id` column.
5. `mavis.channels.outbox_sender.deliver_pending(channel=None, limit=50) -> int` (one-shot delivery for tests/REPL). Outbox repo: `enqueue(session, msg) -> int` (dedupes on `dedupe_key`), `enqueue_now(msg) -> int`, `due(now, limit=20)`, `claim(outbox_id, now) -> bool` (5-minute lease), `mark_sent`, `mark_retry`, `mark_failed`, `to_outbound`.
6. Worker registry: `register_event_handler(event_type, fn, *, replace=False)`, `register_job_handler(kind, fn)`, `clear_handlers()`. **Events** for the same user are serialised under `user_lock`. **Jobs are not serialised**: a job handler that mutates conversation state takes `user_lock` itself. Handlers must be idempotent, because a failing handler causes every handler for that event to re-run on retry.
7. `mavis.api.routes.health.register_readiness_check(name, fn)`.
8. `mavis.agents.persona.split_bubbles(text, max_bubbles=3) -> list[str]`.
9. `mavis.store.db`: `UTCDateTime` (aware-UTC column type), `dispose_engine()`, `ping()`, and a SQLAlchemy naming convention on `Base.metadata`. `mavis.store.migrate.upgrade(url=None, revision="head")`.
10. Users repo extras: `get_by_chat`, `update`, `get_state`, `set_state`, `all_ids`.
11. Extra `Settings` keys: `env`, `llm_timeout_fast_s`, `llm_timeout_smart_s`, `log_json`.
12. **Migration rule for later phases:** add ORM tables to `src/mavis/store/models.py` **and** a new Alembic revision in `src/mavis/migrations/versions/` matching them. `tests/store/test_migrations.py::test_migrations_match_models` enforces this.
13. **Test helpers for all phases** (`tests/conftest.py`, `tests/fakes/`): fixtures `settings`, `db`, `bus`, `channel`, `fake_llm`, plus `tests.fakes.wait_until(predicate, timeout=3.0)`.

## Global Constraints

Inherits `docs/superpowers/plans/2026-10-02-mavis-00-index.md` § Global Constraints. Phase-specific:

- All datetimes stored through `UTCDateTime`; passing a naive datetime raises `ValueError`.
- Webhook mode refuses to start without `TELEGRAM_WEBHOOK_SECRET`.
- The `api` role never calls an LLM.
- Test subpackages each have an `__init__.py` (the `tests` dir is a package so `tests.fakes` is importable).

## Review Focus (owned by this phase)

1. **Duplicate webhook delivery** (index RF #2): the same `update_id` delivered twice publishes one event and gives one reply. Tests: `tests/bus/test_inprocess.py::test_duplicate_event_processed_once`, `tests/bus/test_redis_streams.py::test_publish_dedupes`, `tests/api/test_telegram_webhook.py::test_duplicate_update_published_once`.
2. **Rapid messages** (index RF #3, part 1): two messages for one user are processed strictly in order. Test: `tests/worker/test_runner.py::test_turns_serialised_per_user`.
3. **LLM malformed output / timeout** (index RF #5): `structured()` falls back to JSON mode, then raises `LLMError`. The worker sends a single "give me a sec" message and re-raises so the bus retries. Tests: `tests/llm/test_models.py::test_structured_falls_back_then_raises_llm_error`, `tests/worker/test_runner.py::test_llm_failure_sends_fallback_message`.
4. **Telegram 4096-char limit and 429s**: long replies are split, and a rate limit defers delivery without counting as a failure. Tests: `tests/channels/test_text.py::test_split_long_text_prefers_newlines`, `tests/channels/test_outbox_sender.py::test_rate_limit_defers_without_counting_attempt`.
5. **Retried turn after partial success**: re-running `run_turn` for the same event doesn't duplicate the logged user message, the outbox bubbles, or the assistant log. Test: `tests/agents/test_simple_turn.py::test_run_turn_is_idempotent_on_retry`.

---

### Task 1: Reset the scaffold and configure the project

The scaffold under `src/mavis/` predates the spec and is untracked. Its useful parts (settings, structured output) are re-specified in this plan, and Phase 2 re-specifies graph/vector storage against the `GraphStore`/`VectorStore` contracts. Remove it and set up the project.

**Files:**
- Delete: everything under `src/mavis/` (untracked scaffold)
- Create: `src/mavis/__init__.py`, package `__init__.py` files for `domain`, `llm`, `store`, `store/repo`, `bus`, `channels`, `agents`, `worker`, `api`, `api/routes`
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`, `tests/test_package.py`

**Interfaces:**
- Consumes: nothing
- Produces: importable `mavis` package with `__version__ = "0.1.0"`; console script `mavis` → `mavis.cli:app` (implemented in Task 13)

- [ ] **Step 1: Remove the scaffold and recreate the package skeleton**

```bash
cd /home/jk/Documents/Mavis-AI
git status --porcelain src/   # confirm src/ is untracked (?? src/) before deleting
rm -rf src/mavis
mkdir -p src/mavis/{domain,llm,store/repo,bus,channels,agents,worker,api/routes} tests
for d in src/mavis src/mavis/domain src/mavis/llm src/mavis/store src/mavis/store/repo src/mavis/bus \
         src/mavis/agents src/mavis/worker src/mavis/api src/mavis/api/routes; do
  touch "$d/__init__.py"
done
touch src/mavis/py.typed tests/__init__.py
printf '"""Mavis: a proactive personal assistant agent."""\n\n__version__ = "0.1.0"\n' > src/mavis/__init__.py
```

`src/mavis/channels/__init__.py` and `src/mavis/bus/__init__.py` get real content in Tasks 7 and 8. Create them empty for now:

```bash
touch src/mavis/channels/__init__.py src/mavis/bus/__init__.py
```

- [ ] **Step 2: Adjust dependencies**

```bash
uv remove apscheduler composio
uv add langgraph-checkpoint-postgres langgraph-checkpoint-sqlite "psycopg[binary]" redis alembic \
       pyahocorasick tavily-python boto3 typer
uv add --dev pytest pytest-asyncio respx fakeredis ruff
```

Expected: `uv` resolves and prints `+ typer ...`, `+ fakeredis ...` etc. with no errors.

- [ ] **Step 3: Add project metadata, scripts, pytest and ruff config to `pyproject.toml`**

Change the `description` line and append the sections below. Leave the `dependencies` list as `uv` wrote it.

```toml
description = "Mavis: a 24x7 proactive personal assistant agent"
```

```toml
[project.scripts]
mavis = "mavis.cli:app"

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
pythonpath = ["."]
filterwarnings = ["ignore::DeprecationWarning:telegram.*"]

[tool.ruff]
line-length = 110
target-version = "py313"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ASYNC"]
ignore = ["B008"]  # FastAPI Depends/Header defaults
```

- [ ] **Step 4: Write the smoke test**

`tests/test_package.py`:

```python
import mavis


def test_package_imports_with_version() -> None:
    assert mavis.__version__ == "0.1.0"
```

- [ ] **Step 5: Run it**

Run: `uv run pytest tests/test_package.py -v`
Expected: `1 passed`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src tests
git commit -m "chore: reset scaffold, configure deps, pytest and ruff"
```

---

### Task 2: Settings, logging, and the `settings` fixture

**Files:**
- Create: `src/mavis/config.py`, `src/mavis/logging.py`, `.env.example`
- Create: `tests/conftest.py`, `tests/test_config.py`, `tests/test_logging.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Settings` (all index config keys plus contract addition 11), `get_settings() -> Settings` (lru-cached; creates `data_dir` and `artifacts_dir`), `Settings.db_url`, `Settings.is_sqlite`; `configure_logging(level="INFO")`, `redact_secrets(logger, method, event_dict)`, `REDACTED`; pytest fixture `settings`

- [ ] **Step 1: Write the failing tests**

`tests/conftest.py`:

```python
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
```

`tests/test_config.py`:

```python
from mavis.config import Settings, get_settings


def test_defaults(settings) -> None:
    assert settings.agent_name == "Mavis"
    assert settings.default_timezone == "Asia/Kolkata"
    assert settings.model_fast == "gpt-oss:20b"
    assert settings.model_smart == "gpt-oss:120b"
    assert settings.ollama_base_url == "https://ollama.com/v1"
    assert settings.ping_daily_budget == 6
    assert (settings.quiet_start, settings.quiet_end) == (23, 7)
    assert settings.demo_time_scale == 1.0
    assert settings.is_sqlite
    assert settings.data_dir.exists()
    assert settings.artifacts_dir.exists()


def test_allowed_chat_ids_parse_json(settings, monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_TELEGRAM_CHAT_IDS", "[111, 222]")
    get_settings.cache_clear()
    assert get_settings().allowed_telegram_chat_ids == [111, 222]


def test_db_url_defaults_to_sqlite_in_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(data_dir=tmp_path / "d")
    assert s.db_url == f"sqlite+aiosqlite:///{(tmp_path / 'd' / 'mavis.db').as_posix()}"


def test_postgres_url_is_not_sqlite(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    s = Settings(database_url="postgresql+psycopg://u:p@localhost/mavis")
    assert not s.is_sqlite
```

`tests/test_logging.py`:

```python
import structlog

from mavis.logging import REDACTED, configure_logging, redact_secrets


def test_redacts_secret_like_keys() -> None:
    out = redact_secrets(
        None, "info",
        {"event": "x", "api_key": "abc", "Token": "t", "user_id": 1, "db_password": "p", "client_secret": "s"},
    )
    assert out == {
        "event": "x", "api_key": REDACTED, "Token": REDACTED, "user_id": 1,
        "db_password": REDACTED, "client_secret": REDACTED,
    }


def test_configure_logging_runs(settings, capsys) -> None:
    configure_logging()
    structlog.get_logger().info("hello", telegram_token="should-not-appear")
    err = capsys.readouterr().err
    assert "hello" in err
    assert "should-not-appear" not in err
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_config.py tests/test_logging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.config'`

- [ ] **Step 3: Implement `src/mavis/config.py`**

```python
"""Runtime configuration loaded from the environment / .env. Every config key in the project lives here."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- identity -------------------------------------------------------------
    agent_name: str = "Mavis"  # the agent persona; "Mavis" is the project/package name
    default_timezone: str = "Asia/Kolkata"
    public_base_url: str = "http://localhost:8000"
    env: Literal["dev", "prod", "test"] = "dev"

    # --- LLM (Ollama Cloud, OpenAI-compatible) --------------------------------
    ollama_api_key: str = ""
    ollama_base_url: str = "https://ollama.com/v1"
    model_fast: str = "gpt-oss:20b"
    model_smart: str = "gpt-oss:120b"
    llm_timeout_fast_s: float = 20.0
    llm_timeout_smart_s: float = 60.0

    # --- Telegram -------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_mode: Literal["polling", "webhook"] = "polling"
    allowed_telegram_chat_ids: list[int] = Field(default_factory=list)

    # --- storage --------------------------------------------------------------
    data_dir: Path = Path("data")
    database_url: str = ""  # empty => sqlite in data_dir; prod: postgresql+psycopg://...
    redis_url: str = ""  # empty => in-process bus and locks
    qdrant_url: str = ""  # empty => embedded qdrant in data_dir
    neo4j_uri: str = ""  # empty => sqlite-backed graph fallback
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    artifacts_dir: Path = Path("data/artifacts")

    # --- integrations ---------------------------------------------------------
    composio_api_key: str = ""
    composio_webhook_secret: str = ""
    integration_provider: Literal["composio"] = "composio"
    tavily_api_key: str = ""

    # --- sandbox --------------------------------------------------------------
    sandbox_backend: Literal["auto", "docker", "agentcore", "local"] = "auto"
    # Phase 6 adds the rest (sandbox_runtime, sandboxd_socket, agentcore_*, aws_profile, ...).

    # --- observability --------------------------------------------------------
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    log_json: bool = False

    # --- proactivity ----------------------------------------------------------
    ping_daily_budget: int = 6
    quiet_start: int = 23
    quiet_end: int = 7
    demo_time_scale: float = 1.0

    # --- admin ----------------------------------------------------------------
    admin_user: str = "admin"
    admin_password: str = ""

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{(self.data_dir / 'mavis.db').as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return s
```

- [ ] **Step 4: Implement `src/mavis/logging.py`**

```python
"""structlog setup with secret redaction. Call configure_logging() once per process entrypoint."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from mavis.config import get_settings

_SECRET_KEY = re.compile(r"(?i)(key|token|secret|password)")
REDACTED = "***"


def redact_secrets(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Mask any log field whose name looks like a credential."""
    for key in list(event_dict):
        if key != "event" and _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer() if get_settings().log_json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
```

- [ ] **Step 5: Write `.env.example`**

```dotenv
# --- LLM (Ollama Cloud free tier) ---
OLLAMA_API_KEY=
MODEL_FAST=gpt-oss:20b
MODEL_SMART=gpt-oss:120b

# --- Telegram (create a bot with @BotFather) ---
TELEGRAM_BOT_TOKEN=
TELEGRAM_MODE=polling
TELEGRAM_WEBHOOK_SECRET=
# JSON list of chat ids allowed to talk to the bot (single-user mode). Empty = everyone.
ALLOWED_TELEGRAM_CHAT_IDS=[]

# --- storage (empty = local embedded fallbacks) ---
DATABASE_URL=
REDIS_URL=
QDRANT_URL=
NEO4J_URI=
NEO4J_USER=neo4j
NEO4J_PASSWORD=

# --- integrations / tools ---
COMPOSIO_API_KEY=
COMPOSIO_WEBHOOK_SECRET=
TAVILY_API_KEY=

# --- sandbox (Phase 6): docker (gVisor) via sandboxd sidecar | agentcore | local ---
SANDBOX_BACKEND=auto
SANDBOXD_SOCKET=
AWS_PROFILE=
AGENTCORE_REGION=ap-south-1

# --- observability ---
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com

# --- behaviour ---
DEFAULT_TIMEZONE=Asia/Kolkata
PUBLIC_BASE_URL=http://localhost:8000
PING_DAILY_BUDGET=6
QUIET_START=23
QUIET_END=7
DEMO_TIME_SCALE=1.0
ADMIN_USER=admin
ADMIN_PASSWORD=
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_config.py tests/test_logging.py -v`
Expected: `6 passed`

- [ ] **Step 7: Commit**

```bash
git add src/mavis/config.py src/mavis/logging.py .env.example tests/conftest.py tests/test_config.py tests/test_logging.py
git commit -m "feat(config): settings with all config keys, structlog with secret redaction"
```

---

### Task 3: Domain contracts

Create every domain contract file **verbatim** from the index § Shared Contracts.

**Files:**
- Create: `src/mavis/domain/events.py`, `messages.py`, `memory.py`, `loops.py`, `decisions.py`, `plans.py`, `policy.py`, `integrations.py`, `errors.py`
- Create: `tests/domain/__init__.py`, `tests/domain/test_contracts.py`

**Interfaces:**
- Consumes: nothing
- Produces: all domain types named in the index (`Event`, `EventType`, `Trust`, `Job`, `JobKind`, `Role`, `Button`, `Outbound`, `InboundFile`, `Entity`, `Relation`, `ExtractedEvent`, `LoopDraft`, `ProfileUpdate`, `Extraction`, `RecallContext`, `NODE_LABELS`, `REL_TYPES`, `SINGLE_VALUED_RELS`, `LoopKind`, `LoopStatus`, `WatchSpec`, `Loop`, `LoopUpsert`, `Route`, `RouteDecision`, `NotifyIntent`, `TaskRequest`, `WakeupRequest`, `InitiativeDecision`, `ComposedMessage`, `PlanStep`, `Plan`, `CriticVerdict`, `SlideSpec`, `DeckOutline`, `DocSection`, `DocOutline`, `RiskClass`, `Capability`, `PolicyVerdict`, `UserRef`, `ConnectionState`, `Toolkit`, `ToolResult`, `MavisError`, `LLMError`, `ConnectionRequired`, `ApprovalRequired`, `BudgetExceeded`)

- [ ] **Step 1: Write the failing test**

`tests/domain/__init__.py`: empty file.

`tests/domain/test_contracts.py`:

```python
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from mavis.domain.decisions import (
    ComposedMessage, InitiativeDecision, NotifyIntent, Route, RouteDecision, TaskRequest, WakeupRequest,
)
from mavis.domain.errors import ApprovalRequired, BudgetExceeded, ConnectionRequired, LLMError, MavisError
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.domain.integrations import ConnectionState, Toolkit, ToolResult, UserRef
from mavis.domain.loops import Loop, LoopKind, LoopStatus, LoopUpsert, WatchSpec
from mavis.domain.memory import (
    REL_TYPES, SINGLE_VALUED_RELS, Entity, ExtractedEvent, Extraction, LoopDraft, ProfileUpdate,
    RecallContext, Relation,
)
from mavis.domain.messages import Button, InboundFile, Outbound, Role
from mavis.domain.plans import CriticVerdict, DeckOutline, DocOutline, DocSection, Plan, PlanStep, SlideSpec
from mavis.domain.policy import Capability, PolicyVerdict, RiskClass

NOW = datetime(2026, 10, 2, 4, 30, tzinfo=UTC)

SAMPLES: list[BaseModel] = [
    Event(id="tg:update:1", user_id=1, type=EventType.USER_MESSAGE, occurred_at=NOW, source="telegram",
          payload={"text": "hi"}, trust=Trust.USER),
    Job(id="j1", user_id=1, kind=JobKind.LEARN, payload={"text": "x"}),
    Outbound(user_id=1, text="hey", buttons=[[Button(label="OK", data="ok")]], dedupe_key="k"),
    InboundFile(file_id="f", file_name="a.pdf", mime_type="application/pdf", size=10),
    Extraction(
        entities=[Entity(name="Jawahar", label="Person", aliases=["Jawa"])],
        relations=[Relation(subject="User", rel="FRIEND_OF", object="Jawahar",
                            statement="Jawahar is the user's friend.")],
        events=[ExtractedEvent(title="Interview prep", starts_at=NOW, with_people=["Jawahar"], importance=4)],
        loops=[LoopDraft(kind="COMMITMENT", title="Interview prep", due_at=NOW)],
        profile_updates=[ProfileUpdate(field="name", value="Jai")],
        mood="anxious",
    ),
    Loop(id=1, user_id=1, kind=LoopKind.WATCH, title="Reply from recruiter",
         watch=WatchSpec(from_contains="recruiter", keywords=["offer"], deadline=NOW)),
    InitiativeDecision(
        notify=NotifyIntent(urgency=3, intent="pep talk", dedupe_key="loop:1:pre"),
        act=[TaskRequest(goal="draft a reply")],
        track=[LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview prep", due_at=NOW)],
        wakeups=[WakeupRequest(at=NOW, reason="pep talk", loop_id=1)],
    ),
    RouteDecision(route=Route.TASK, reason="needs research"),
    ComposedMessage(send=True, messages=["a", "b"]),
    Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="i")], deliverable="pptx"),
    CriticVerdict(accept=False, revise_steps=["s1"], feedback="needs sources"),
    DeckOutline(title="t", slides=[SlideSpec(title="s", bullets=["b"])]),
    DocOutline(title="d", sections=[DocSection(heading="h", table=[["a", "b"]])]),
    PolicyVerdict(allow=False, defer_until=NOW, reason="quiet hours"),
    Toolkit(slug="gmail", name="Gmail", description="mail"),
    ToolResult(ok=True, data={"a": 1}),
]


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_json_round_trip(obj: BaseModel) -> None:
    assert type(obj).model_validate_json(obj.model_dump_json()) == obj


def test_enum_values_are_stable() -> None:
    assert Role.USER == "user"
    assert ConnectionState.ACTIVE == "ACTIVE"
    assert LoopStatus.OPEN == "OPEN"
    assert Capability.CALENDAR == "googlecalendar"
    assert "RELATED_TO" in REL_TYPES
    assert "WORKS_AT" in SINGLE_VALUED_RELS


def test_risk_classes_needing_approval() -> None:
    assert not RiskClass.READ.needs_approval
    assert not RiskClass.WRITE_SELF.needs_approval
    assert RiskClass.OUTWARD.needs_approval
    assert RiskClass.SPEND.needs_approval
    assert RiskClass.DESTRUCTIVE.needs_approval


def test_user_ref_provider_id() -> None:
    assert UserRef(user_id=7).provider_id == "mavis-7"


def test_recall_render_sections() -> None:
    ctx = RecallContext(profile="Jai, job hunting", loops=["Interview Monday"], facts=["Jawahar is a friend"])
    text = ctx.render()
    assert "## About the user\nJai, job hunting" in text
    assert "## Open loops\n- Interview Monday" in text
    assert "## Known facts\n- Jawahar is a friend" in text
    assert "Related past moments" not in text
    assert RecallContext().render() == ""


def test_errors_carry_details() -> None:
    e = ConnectionRequired(Capability.GMAIL, "need inbox access")
    assert e.capability is Capability.GMAIL and e.reason == "need inbox access" and "gmail" in str(e)
    a = ApprovalRequired("mail.send", "To: x", {"to": "x"})
    assert a.action == "mail.send" and a.arguments == {"to": "x"}
    assert issubclass(LLMError, MavisError) and issubclass(BudgetExceeded, MavisError)


def test_button_data_limit() -> None:
    with pytest.raises(ValidationError):
        Button(label="x", data="a" * 65)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/domain/test_contracts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.domain.decisions'`

- [ ] **Step 3: Create the nine domain files verbatim**

Copy each code block from the index § Shared Contracts into the matching file, character for character:

| Index block | File |
|---|---|
| `src/mavis/domain/events.py` | `src/mavis/domain/events.py` |
| `src/mavis/domain/messages.py` | `src/mavis/domain/messages.py` |
| `src/mavis/domain/memory.py` | `src/mavis/domain/memory.py` |
| `src/mavis/domain/loops.py` | `src/mavis/domain/loops.py` |
| `src/mavis/domain/decisions.py` | `src/mavis/domain/decisions.py` (it imports `LoopUpsert` from `mavis.domain.loops` at the top) |
| `src/mavis/domain/plans.py` | `src/mavis/domain/plans.py` |
| `src/mavis/domain/policy.py` | `src/mavis/domain/policy.py` |
| `src/mavis/domain/integrations.py` | `src/mavis/domain/integrations.py` |
| `src/mavis/domain/errors.py` | `src/mavis/domain/errors.py` |

Do not add, rename or reorder fields.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/domain/test_contracts.py -v`
Expected: `22 passed` (16 round-trip cases + 6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mavis/domain tests/domain
git commit -m "feat(domain): shared contract types for events, memory, loops, decisions, plans, policy"
```

---

### Task 4: Database layer, P1 tables, repositories, and the `db` fixture

**Files:**
- Create: `src/mavis/store/db.py`, `src/mavis/store/models.py`
- Create: `src/mavis/store/repo/users.py`, `messages.py`, `outbox.py`, `events.py`
- Modify: `tests/conftest.py` (append the `db` fixture)
- Create: `tests/store/__init__.py`, `tests/store/test_repos.py`

**Interfaces:**
- Consumes: `get_settings()`, `Role`, `Outbound`, `Button`
- Produces:
  - `mavis.store.db`: `utcnow() -> datetime`, `UTCDateTime`, `Base`, `get_engine() -> AsyncEngine`, `Session` (call it: `async with Session() as s`), `init_db()`, `ping() -> bool`, `dispose_engine()`
  - `mavis.store.models`: `User`, `Message`, `OutboxMessage`, `ProcessedEvent`
  - `users`: `get_or_create_by_chat(chat_id, name) -> tuple[User, bool]`, `get(user_id) -> User`, `get_by_chat(chat_id) -> User | None`, `update(user_id, **fields)`, `get_state(user_id) -> dict`, `set_state(user_id, **kv)`, `all_ids() -> list[int]`
  - `messages`: `log(user_id, role, content, proactive=False, event_id=None) -> bool`, `recent(user_id, limit=20) -> list[Message]`
  - `outbox`: `enqueue(session, msg) -> int`, `enqueue_now(msg) -> int`, `due(now, limit=20) -> list[OutboxMessage]`, `claim(outbox_id, now) -> bool`, `mark_sent(outbox_id, provider_ids)`, `mark_retry(outbox_id, error, next_attempt_at, count_attempt=True)`, `mark_failed(outbox_id, error)`, `to_outbound(row) -> Outbound`, `LEASE`
  - `events`: `claim(session, event_id) -> bool`
  - fixture `db`

- [ ] **Step 1: Append the `db` fixture to `tests/conftest.py`**

```python
@pytest.fixture
async def db(settings):
    """Fresh SQLite schema per test (create_all). Disposes the engine afterwards."""
    from mavis.store.db import dispose_engine, init_db

    await dispose_engine()
    await init_db()
    yield
    await dispose_engine()
```

- [ ] **Step 2: Write the failing tests**

`tests/store/__init__.py`: empty file.

`tests/store/test_repos.py`:

```python
from datetime import datetime, timedelta

import pytest
from sqlalchemy.dialects import sqlite

from mavis.domain.messages import Button, Outbound, Role
from mavis.store.db import Session, UTCDateTime, utcnow
from mavis.store.repo import events, messages, outbox, users


async def test_get_or_create_by_chat_is_idempotent(db) -> None:
    u1, created1 = await users.get_or_create_by_chat(42, "Jai")
    u2, created2 = await users.get_or_create_by_chat(42, "Someone else")
    assert created1 and not created2
    assert u1.id == u2.id
    assert u2.name == "Jai"
    assert u1.timezone == "Asia/Kolkata"
    assert await users.get_by_chat(42) is not None
    assert await users.get_by_chat(43) is None
    assert await users.all_ids() == [u1.id]


async def test_update_and_state(db) -> None:
    u, _ = await users.get_or_create_by_chat(1, None)
    await users.update(u.id, name="Jai", onboarded=True)
    await users.set_state(u.id, gmail_cursor="abc")
    await users.set_state(u.id, other=1)
    fresh = await users.get(u.id)
    assert fresh.name == "Jai" and fresh.onboarded
    assert fresh.state == {"gmail_cursor": "abc", "other": 1}
    assert await users.get_state(u.id) == {"gmail_cursor": "abc", "other": 1}


async def test_messages_log_recent_and_dedupe(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    assert await messages.log(u.id, Role.USER, "hi", event_id="e1")
    assert not await messages.log(u.id, Role.USER, "hi", event_id="e1")
    assert await messages.log(u.id, Role.ASSISTANT, "hey")
    rows = await messages.recent(u.id)
    assert [(r.role, r.content) for r in rows] == [("user", "hi"), ("assistant", "hey")]
    fresh = await users.get(u.id)
    assert fresh.last_user_msg_at is not None and fresh.last_user_msg_at.tzinfo is not None
    assert fresh.last_agent_msg_at is not None


async def test_recent_respects_limit_and_order(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    for i in range(5):
        await messages.log(u.id, Role.USER, f"m{i}")
    assert [r.content for r in await messages.recent(u.id, limit=3)] == ["m2", "m3", "m4"]


async def test_outbox_enqueue_dedupes(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    async with Session() as s:
        a = await outbox.enqueue(s, Outbound(user_id=u.id, text="x", dedupe_key="k"))
        b = await outbox.enqueue(s, Outbound(user_id=u.id, text="x", dedupe_key="k"))
        await s.commit()
    assert a == b
    assert len(await outbox.due(utcnow())) == 1


async def test_outbox_claim_is_exclusive_with_lease(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    oid = await outbox.enqueue_now(Outbound(user_id=u.id, text="x"))
    now = utcnow()
    assert await outbox.claim(oid, now)
    assert not await outbox.claim(oid, now)
    assert await outbox.due(now) == []
    later = now + outbox.LEASE + timedelta(seconds=1)  # a crashed sender's lease expires
    assert [r.id for r in await outbox.due(later)] == [oid]


async def test_outbox_mark_retry_sent_failed(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    oid = await outbox.enqueue_now(Outbound(user_id=u.id, text="x"))
    now = utcnow()
    await outbox.mark_retry(oid, "boom", now + timedelta(seconds=10))
    assert await outbox.due(now) == []
    [row] = await outbox.due(now + timedelta(seconds=11))
    assert row.attempts == 1 and row.last_error == "boom" and row.status == "pending"
    await outbox.mark_retry(oid, "rate", now, count_attempt=False)
    [row] = await outbox.due(now)
    assert row.attempts == 1
    await outbox.mark_sent(oid, [7, 8])
    assert await outbox.due(now + timedelta(days=1)) == []
    oid2 = await outbox.enqueue_now(Outbound(user_id=u.id, text="y"))
    await outbox.mark_failed(oid2, "dead")
    assert await outbox.due(now + timedelta(days=1)) == []


async def test_outbox_round_trip_to_outbound(db) -> None:
    u, _ = await users.get_or_create_by_chat(42, "Jai")
    msg = Outbound(user_id=u.id, text="Send?", buttons=[[Button(label="✅ Send", data="appr:1:yes")]],
                   proactive=True, dedupe_key="d1")
    await outbox.enqueue_now(msg)
    [row] = await outbox.due(utcnow())
    assert outbox.to_outbound(row) == msg


async def test_processed_event_claim_once(db) -> None:
    async with Session() as s:
        assert await events.claim(s, "e1")
        await s.commit()
    async with Session() as s:
        assert not await events.claim(s, "e1")


def test_utc_datetime_rejects_naive() -> None:
    with pytest.raises(ValueError):
        UTCDateTime().process_bind_param(datetime(2026, 1, 1), sqlite.dialect())
```

- [ ] **Step 3: Run the tests to confirm they fail**

Run: `uv run pytest tests/store/test_repos.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.store.db'`

- [ ] **Step 4: Implement `src/mavis/store/db.py`**

```python
"""Engine/session management. SQLite (aiosqlite) in dev/test, Postgres (psycopg 3) in prod."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, text
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from mavis.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC in Python regardless of backend (SQLite stores naive UTC)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime; use timezone-aware datetimes")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: UTCDateTime}


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        url = get_settings().db_url
        kwargs: dict[str, Any] = (
            {"connect_args": {"timeout": 30}}
            if url.startswith("sqlite")
            else {"pool_size": 10, "max_overflow": 10, "pool_pre_ping": True}
        )
        _engine = create_async_engine(url, **kwargs)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


class _SessionFactory:
    """`async with Session() as s:` — resolves the engine lazily so tests can swap databases."""

    def __call__(self) -> AsyncSession:
        get_engine()
        assert _sessionmaker is not None
        return _sessionmaker()


Session = _SessionFactory()


async def init_db() -> None:
    """Create all tables directly (dev/test). Production schemas are managed by `mavis migrate`."""
    from mavis.store import models  # noqa: F401  (registers tables on Base.metadata)

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ping() -> bool:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
```

- [ ] **Step 5: Implement `src/mavis/store/models.py`**

```python
"""ORM tables. Later phases append their tables here AND add a matching Alembic revision."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mavis.store.db import Base, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64))
    onboarded: Mapped[bool] = mapped_column(default=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_user_msg_at: Mapped[datetime | None] = mapped_column(default=None)
    last_agent_msg_at: Mapped[datetime | None] = mapped_column(default=None)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    proactive: Mapped[bool] = mapped_column(default=False)
    event_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class OutboxMessage(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    buttons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    document_path: Mapped[str | None] = mapped_column(String(1024))
    proactive: Mapped[bool] = mapped_column(default=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    provider_message_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(default=utcnow)
```

- [ ] **Step 6: Implement the repositories**

`src/mavis/store/repo/users.py`:

```python
from __future__ import annotations

from typing import Any

from sqlalchemy import select, update as sa_update
from sqlalchemy.exc import IntegrityError

from mavis.config import get_settings
from mavis.store.db import Session
from mavis.store.models import User


async def get_or_create_by_chat(chat_id: int, name: str | None) -> tuple[User, bool]:
    async with Session() as s:
        user = await s.scalar(select(User).where(User.telegram_chat_id == chat_id))
        if user is not None:
            return user, False
        user = User(telegram_chat_id=chat_id, name=name, timezone=get_settings().default_timezone, state={})
        s.add(user)
        try:
            await s.commit()
        except IntegrityError:  # another worker created it concurrently
            await s.rollback()
            existing = await s.scalar(select(User).where(User.telegram_chat_id == chat_id))
            assert existing is not None
            return existing, False
        return user, True


async def get(user_id: int) -> User:
    async with Session() as s:
        return await s.get_one(User, user_id)


async def get_by_chat(chat_id: int) -> User | None:
    async with Session() as s:
        return await s.scalar(select(User).where(User.telegram_chat_id == chat_id))


async def update(user_id: int, **fields: Any) -> None:
    async with Session() as s:
        await s.execute(sa_update(User).where(User.id == user_id).values(**fields))
        await s.commit()


async def get_state(user_id: int) -> dict[str, Any]:
    async with Session() as s:
        user = await s.get_one(User, user_id)
        return dict(user.state or {})


async def set_state(user_id: int, **kv: Any) -> None:
    async with Session() as s:
        user = await s.get_one(User, user_id)
        user.state = {**(user.state or {}), **kv}
        await s.commit()


async def all_ids() -> list[int]:
    async with Session() as s:
        return list(await s.scalars(select(User.id).order_by(User.id)))
```

`src/mavis/store/repo/messages.py`:

```python
from __future__ import annotations

from sqlalchemy import select, update

from mavis.domain.messages import Role
from mavis.store.db import Session, utcnow
from mavis.store.models import Message, User


async def log(
    user_id: int, role: Role, content: str, proactive: bool = False, event_id: str | None = None
) -> bool:
    """Append to the conversation log. Returns False if `event_id` was already logged (retry)."""
    now = utcnow()
    async with Session() as s:
        if event_id and await s.scalar(select(Message.id).where(Message.event_id == event_id)):
            return False
        s.add(Message(user_id=user_id, role=role.value, content=content, proactive=proactive,
                      event_id=event_id, created_at=now))
        column = "last_user_msg_at" if role is Role.USER else "last_agent_msg_at"
        await s.execute(update(User).where(User.id == user_id).values({column: now}))
        await s.commit()
        return True


async def recent(user_id: int, limit: int = 20) -> list[Message]:
    """The last `limit` messages, oldest first."""
    async with Session() as s:
        rows = await s.scalars(
            select(Message).where(Message.user_id == user_id)
            .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        )
        return list(reversed(list(rows)))
```

`src/mavis/store/repo/outbox.py`:

```python
"""Transactional outbox: every message to the user is a row first, delivered by OutboxSender."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mavis.domain.messages import Button, Outbound
from mavis.store.db import Session, utcnow
from mavis.store.models import OutboxMessage

LEASE = timedelta(seconds=300)  # a claimed row becomes due again if its sender dies
_DELIVERABLE = ("pending", "sending")


async def enqueue(session: AsyncSession, msg: Outbound) -> int:
    """Insert within the caller's transaction. Same dedupe_key => existing row id, no new row."""
    if msg.dedupe_key:
        existing = await session.scalar(
            select(OutboxMessage.id).where(OutboxMessage.dedupe_key == msg.dedupe_key)
        )
        if existing is not None:
            return existing
    row = OutboxMessage(
        user_id=msg.user_id,
        text=msg.text,
        buttons=[[b.model_dump() for b in row] for row in msg.buttons],
        document_path=msg.document_path,
        proactive=msg.proactive,
        dedupe_key=msg.dedupe_key,
    )
    session.add(row)
    await session.flush()
    return row.id


async def enqueue_now(msg: Outbound) -> int:
    async with Session() as s:
        outbox_id = await enqueue(s, msg)
        await s.commit()
        return outbox_id


async def due(now: datetime, limit: int = 20) -> list[OutboxMessage]:
    async with Session() as s:
        rows = await s.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.status.in_(_DELIVERABLE), OutboxMessage.next_attempt_at <= now)
            .order_by(OutboxMessage.id).limit(limit)
        )
        return list(rows)


async def claim(outbox_id: int, now: datetime) -> bool:
    """Atomically take a row for delivery. Only one sender wins."""
    async with Session() as s:
        res = await s.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == outbox_id, OutboxMessage.status.in_(_DELIVERABLE),
                   OutboxMessage.next_attempt_at <= now)
            .values(status="sending", next_attempt_at=now + LEASE)
        )
        await s.commit()
        return res.rowcount == 1


async def mark_sent(outbox_id: int, provider_ids: list[int]) -> None:
    async with Session() as s:
        await s.execute(
            update(OutboxMessage).where(OutboxMessage.id == outbox_id)
            .values(status="sent", sent_at=utcnow(), provider_message_ids=provider_ids, last_error=None)
        )
        await s.commit()


async def mark_retry(outbox_id: int, error: str, next_attempt_at: datetime, count_attempt: bool = True) -> None:
    values: dict = {"status": "pending", "last_error": error, "next_attempt_at": next_attempt_at}
    if count_attempt:
        values["attempts"] = OutboxMessage.attempts + 1
    async with Session() as s:
        await s.execute(update(OutboxMessage).where(OutboxMessage.id == outbox_id).values(**values))
        await s.commit()


async def mark_failed(outbox_id: int, error: str) -> None:
    async with Session() as s:
        await s.execute(
            update(OutboxMessage).where(OutboxMessage.id == outbox_id)
            .values(status="failed", last_error=error, attempts=OutboxMessage.attempts + 1)
        )
        await s.commit()


def to_outbound(row: OutboxMessage) -> Outbound:
    return Outbound(
        user_id=row.user_id,
        text=row.text,
        buttons=[[Button(**b) for b in r] for r in row.buttons or []],
        document_path=row.document_path,
        proactive=row.proactive,
        dedupe_key=row.dedupe_key,
    )
```

`src/mavis/store/repo/events.py`:

```python
"""Exactly-once guard for side-effecting handlers (in addition to bus-level dedupe)."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mavis.store.db import utcnow
from mavis.store.models import ProcessedEvent


async def claim(session: AsyncSession, event_id: str) -> bool:
    """Record event_id as processed within the caller's transaction. False if already recorded."""
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    stmt = insert(ProcessedEvent).values(id=event_id, processed_at=utcnow()).on_conflict_do_nothing(
        index_elements=["id"]
    )
    res = await session.execute(stmt)
    return res.rowcount == 1
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/store/test_repos.py -v`
Expected: `10 passed`

- [ ] **Step 8: Commit**

```bash
git add src/mavis/store tests/store tests/conftest.py
git commit -m "feat(store): async engine, UTC datetimes, P1 tables, user/message/outbox/event repos"
```

---

### Task 5: Alembic migrations

**Files:**
- Create: `alembic.ini`, `src/mavis/migrations/env.py`, `src/mavis/migrations/script.py.mako`, `src/mavis/migrations/versions/0001_foundation.py`, `src/mavis/store/migrate.py`
- Create: `tests/store/test_migrations.py`

**Interfaces:**
- Consumes: `Base.metadata`, `mavis.store.models`, `get_settings().db_url`
- Produces: `mavis.store.migrate.upgrade(url: str | None = None, revision: str = "head") -> None` (sync; uses `asyncio.run` internally, so call it from a thread when a loop is running). Revision id `"0001"`.

- [ ] **Step 1: Write the failing tests**

`tests/store/test_migrations.py`:

```python
import sqlite3

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from mavis.store import models  # noqa: F401
from mavis.store.db import Base
from mavis.store.migrate import upgrade


def test_upgrade_creates_tables(tmp_path) -> None:
    db_file = tmp_path / "m.db"
    upgrade(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    names = {r[0] for r in sqlite3.connect(db_file).execute("select name from sqlite_master where type='table'")}
    assert {"users", "messages", "outbox", "processed_events", "alembic_version"} <= names


def test_migrations_match_models(tmp_path) -> None:
    """Guards every later phase: a new ORM table without a matching revision fails here."""
    db_file = tmp_path / "m.db"
    upgrade(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `uv run pytest tests/store/test_migrations.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.store.migrate'`

- [ ] **Step 3: Create `alembic.ini` at the repo root**

```ini
[alembic]
script_location = src/mavis/migrations
prepend_sys_path = src
file_template = %%(rev)s_%%(slug)s

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

- [ ] **Step 4: Create `src/mavis/migrations/env.py`**

```python
"""Alembic environment (async). URL comes from config.attributes['url'] or Settings.db_url."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from mavis.config import get_settings
from mavis.store import models  # noqa: F401  (registers tables)
from mavis.store.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.attributes.get("url") or get_settings().db_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 5: Create `src/mavis/migrations/script.py.mako`**

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 6: Create `src/mavis/migrations/versions/0001_foundation.py`**

```python
"""foundation: users, messages, outbox, processed_events

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("onboarded", sa.Boolean(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_user_msg_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_agent_msg_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_telegram_chat_id"), "users", ["telegram_chat_id"], unique=True)

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("proactive", sa.Boolean(), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_messages_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("event_id", name=op.f("uq_messages_event_id")),
    )
    op.create_index(op.f("ix_messages_user_id"), "messages", ["user_id"], unique=False)
    op.create_index(op.f("ix_messages_created_at"), "messages", ["created_at"], unique=False)

    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("buttons", sa.JSON(), nullable=False),
        sa.Column("document_path", sa.String(length=1024), nullable=True),
        sa.Column("proactive", sa.Boolean(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_message_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_outbox_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_outbox_dedupe_key")),
    )
    op.create_index(op.f("ix_outbox_user_id"), "outbox", ["user_id"], unique=False)
    op.create_index(op.f("ix_outbox_status"), "outbox", ["status"], unique=False)
    op.create_index(op.f("ix_outbox_next_attempt_at"), "outbox", ["next_attempt_at"], unique=False)

    op.create_table(
        "processed_events",
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processed_events")),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_index(op.f("ix_outbox_next_attempt_at"), table_name="outbox")
    op.drop_index(op.f("ix_outbox_status"), table_name="outbox")
    op.drop_index(op.f("ix_outbox_user_id"), table_name="outbox")
    op.drop_table("outbox")
    op.drop_index(op.f("ix_messages_created_at"), table_name="messages")
    op.drop_index(op.f("ix_messages_user_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_users_telegram_chat_id"), table_name="users")
    op.drop_table("users")
```

- [ ] **Step 7: Create `src/mavis/store/migrate.py`**

```python
"""Programmatic Alembic upgrade (used by `mavis migrate` and tests). Synchronous by design."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def upgrade(url: str | None = None, revision: str = "head") -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.attributes["url"] = url
    command.upgrade(cfg, revision)
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/store/test_migrations.py -v`
Expected: `2 passed`. If `test_migrations_match_models` reports a diff, the migration and `models.py` disagree. Fix the migration, not the test.

- [ ] **Step 9: Commit**

```bash
git add alembic.ini src/mavis/migrations src/mavis/store/migrate.py tests/store/test_migrations.py
git commit -m "feat(store): alembic migrations with model/migration drift test"
```

---

### Task 6: LLM layer and the `fake_llm` fixture

**Files:**
- Create: `src/mavis/llm/tracing.py`, `src/mavis/llm/models.py`
- Create: `tests/fakes/__init__.py`, `tests/fakes/llm.py`
- Modify: `tests/conftest.py` (append `fake_llm`)
- Create: `tests/llm/__init__.py`, `tests/llm/test_models.py`

**Interfaces:**
- Consumes: `get_settings()`, `LLMError`
- Produces:
  - `mavis.llm.tracing.callbacks() -> list`
  - `mavis.llm.models`: `Tier`, `chat_model(tier=Tier.FAST, temperature=0.6) -> BaseChatModel`, `complete(messages, tier=Tier.FAST, temperature=0.6, name="complete") -> str`, `structured(schema, system, user, tier=Tier.FAST) -> T`, `run_config(name) -> dict`
  - **Calling convention for every phase:** `from mavis.llm import models as llm`, then `llm.structured(...)`, `llm.complete(...)`, `llm.chat_model(...)`
  - `tests.fakes.llm`: `FakeLLM` (`push_ai(AIMessage)`, `push_text(str)`, `push_structured(BaseModel)`, `push_error(exc, structured=False)`, `pop_ai()`, `pop_structured(schema=None)`, `.calls`, `.structured_calls`, `chat_model(tier, temperature)`, `async structured(schema, system, user, tier)`), `FakeToolChatModel` (supports `bind_tools`, `with_structured_output`, `.bound_tools`)
  - `tests.fakes.wait_until(predicate, timeout=3.0)`
  - fixture `fake_llm` (patches `mavis.llm.models.chat_model` and `mavis.llm.models.structured`)

- [ ] **Step 1: Create the fakes**

`tests/fakes/llm.py`:

```python
"""Scripted LLM doubles. Tests queue responses; any unexpected extra call fails loudly."""

from __future__ import annotations

from collections import deque
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, ConfigDict


class FakeLLM:
    def __init__(self) -> None:
        self.ai_queue: deque[AIMessage | Exception] = deque()
        self.structured_queue: deque[BaseModel | Exception] = deque()
        self.calls: list[list[BaseMessage]] = []
        self.structured_calls: list[dict[str, Any]] = []

    # --- scripting -----------------------------------------------------------
    def push_ai(self, msg: AIMessage) -> None:
        self.ai_queue.append(msg)

    def push_text(self, text: str) -> None:
        self.ai_queue.append(AIMessage(content=text))

    def push_structured(self, obj: BaseModel) -> None:
        self.structured_queue.append(obj)

    def push_error(self, exc: Exception, structured: bool = False) -> None:
        (self.structured_queue if structured else self.ai_queue).append(exc)

    # --- consumption ---------------------------------------------------------
    def pop_ai(self) -> AIMessage:
        if not self.ai_queue:
            raise AssertionError("FakeLLM: no scripted AI message left")
        item = self.ai_queue.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def pop_structured(self, schema: type[BaseModel] | None = None) -> BaseModel:
        if not self.structured_queue:
            raise AssertionError("FakeLLM: no scripted structured output left")
        item = self.structured_queue.popleft()
        if isinstance(item, Exception):
            raise item
        if schema is not None and not isinstance(item, schema):
            raise AssertionError(f"FakeLLM: expected {schema.__name__}, got {type(item).__name__}")
        return item

    # --- drop-in replacements for mavis.llm.models ----------------------------
    def chat_model(self, tier: Any = None, temperature: float = 0.6) -> FakeToolChatModel:
        return FakeToolChatModel(fake=self)

    async def structured(self, schema: type[BaseModel], system: str, user: Any, tier: Any = None) -> BaseModel:
        self.structured_calls.append({"schema": schema, "system": system, "user": user, "tier": tier})
        return self.pop_structured(schema)


class FakeToolChatModel(BaseChatModel):
    """A chat model that returns queued AIMessages (with or without tool_calls).

    Works inside LangGraph ReAct agents: bind_tools() returns a copy that remembers the tools.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    fake: Any
    bound_tools: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake-tool-chat"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.fake.calls.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.fake.pop_ai())])

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolChatModel:  # type: ignore[override]
        return FakeToolChatModel(fake=self.fake, bound_tools=list(tools))

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable:  # type: ignore[override]
        target = schema if isinstance(schema, type) else None
        return RunnableLambda(lambda _input: self.fake.pop_structured(target))
```

`tests/fakes/__init__.py`:

```python
"""Test doubles shared across phases."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from tests.fakes.llm import FakeLLM, FakeToolChatModel

__all__ = ["FakeLLM", "FakeToolChatModel", "wait_until"]


async def wait_until(predicate: Callable[[], bool | Awaitable[bool]], timeout: float = 3.0) -> None:
    """Poll `predicate` (sync or async) until truthy, else fail the test after `timeout` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if loop.time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.01)
```

Append to `tests/conftest.py`:

```python
@pytest.fixture
def fake_llm(monkeypatch):
    """Replaces mavis.llm.models.chat_model and .structured with a scripted FakeLLM."""
    from tests.fakes.llm import FakeLLM
    from mavis.llm import models

    fake = FakeLLM()
    monkeypatch.setattr(models, "chat_model", fake.chat_model)
    monkeypatch.setattr(models, "structured", fake.structured)
    return fake
```

- [ ] **Step 2: Write the failing tests**

`tests/llm/__init__.py`: empty file.

`tests/llm/test_models.py`:

```python
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from tests.fakes.llm import FakeLLM
from mavis.domain.errors import LLMError
from mavis.llm import models
from mavis.llm.models import Tier


class Sample(BaseModel):
    name: str
    n: int


@pytest.fixture
def scripted(monkeypatch) -> FakeLLM:
    """Patch only chat_model so the REAL structured()/complete() run against scripted outputs."""
    fake = FakeLLM()
    monkeypatch.setattr(models, "chat_model", fake.chat_model)
    return fake


async def test_structured_tool_mode_returns_object(settings, scripted) -> None:
    scripted.push_structured(Sample(name="a", n=1))
    assert await models.structured(Sample, "sys", "user") == Sample(name="a", n=1)


async def test_structured_falls_back_to_json_mode(settings, scripted) -> None:
    scripted.push_error(ValueError("tool calling unsupported"), structured=True)
    scripted.push_text('Sure! {"name": "x", "n": 2} hope that helps')
    assert await models.structured(Sample, "sys", "user") == Sample(name="x", n=2)
    # the JSON-mode prompt carried the schema hint as the last message
    assert "JSON schema" in scripted.calls[-1][-1].content


async def test_structured_falls_back_then_raises_llm_error(settings, scripted) -> None:
    scripted.push_error(ValueError("tool calling unsupported"), structured=True)
    scripted.push_text("not json at all")
    scripted.push_text('{"name": "x"}')  # missing field -> still invalid
    with pytest.raises(LLMError, match="Sample"):
        await models.structured(Sample, "sys", "user")


async def test_structured_accepts_message_list(settings, scripted) -> None:
    scripted.push_structured(Sample(name="a", n=1))
    await models.structured(Sample, "sys", [HumanMessage("hi")])


async def test_complete_returns_text_and_wraps_errors(settings, scripted) -> None:
    scripted.push_text("  hello  ")
    assert await models.complete([SystemMessage("s"), HumanMessage("h")]) == "hello"
    scripted.push_error(TimeoutError())
    with pytest.raises(LLMError):
        await models.complete([HumanMessage("h")])
    scripted.push_text("   ")
    with pytest.raises(LLMError, match="empty"):
        await models.complete([HumanMessage("h")])


def test_chat_model_uses_tier_models(settings) -> None:
    assert models.chat_model(Tier.FAST).model_name == "gpt-oss:20b"
    assert models.chat_model(Tier.SMART).model_name == "gpt-oss:120b"


async def test_fake_llm_fixture_patches_module(fake_llm) -> None:
    fake_llm.push_structured(Sample(name="z", n=9))
    assert await models.structured(Sample, "s", "u") == Sample(name="z", n=9)
    assert fake_llm.structured_calls[0]["schema"] is Sample
```

- [ ] **Step 3: Run them to confirm they fail**

Run: `uv run pytest tests/llm/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.llm.models'`

- [ ] **Step 4: Implement `src/mavis/llm/tracing.py`**

```python
"""Optional Langfuse tracing: no-op unless both keys are configured."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import structlog

from mavis.config import get_settings

log = structlog.get_logger(__name__)


@lru_cache
def _handler() -> Any | None:
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", s.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", s.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", s.langfuse_host)
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as exc:  # noqa: BLE001 - tracing must never break the agent
        log.warning("langfuse.disabled", error=str(exc))
        return None


def callbacks() -> list[Any]:
    handler = _handler()
    return [handler] if handler else []
```

- [ ] **Step 5: Implement `src/mavis/llm/models.py`**

```python
"""Model routing (FAST / SMART) and validated structured output.

Callers MUST use `from mavis.llm import models as llm` and call `llm.structured(...)` etc. so tests can
monkeypatch this module. Internal calls go through the module-global `chat_model`, so patching it
also fakes `complete()` and the JSON fallback in `structured()`.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from functools import lru_cache
from typing import Any, TypeVar

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from mavis.config import get_settings
from mavis.domain.errors import LLMError
from mavis.llm.tracing import callbacks

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class Tier(StrEnum):
    FAST = "fast"
    SMART = "smart"


@lru_cache(maxsize=16)
def _build(model: str, base_url: str, api_key: str, temperature: float, timeout: float) -> ChatOpenAI:
    return ChatOpenAI(
        model=model, base_url=base_url, api_key=api_key or "missing",
        temperature=temperature, timeout=timeout, max_retries=2,
    )


def chat_model(tier: Tier = Tier.FAST, temperature: float = 0.6) -> BaseChatModel:
    s = get_settings()
    fast = tier is Tier.FAST
    return _build(
        s.model_fast if fast else s.model_smart,
        s.ollama_base_url,
        s.ollama_api_key,
        temperature,
        s.llm_timeout_fast_s if fast else s.llm_timeout_smart_s,
    )


def run_config(name: str) -> dict[str, Any]:
    return {"callbacks": callbacks(), "run_name": name}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)


async def complete(
    messages: list[BaseMessage], tier: Tier = Tier.FAST, temperature: float = 0.6, name: str = "complete"
) -> str:
    """Plain-text completion. Raises LLMError on failure or empty output."""
    try:
        out = await chat_model(tier, temperature).ainvoke(messages, config=run_config(name))
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"{tier.value} model call failed: {type(exc).__name__}") from exc
    text = _text_of(out.content).strip()
    if not text:
        raise LLMError(f"{tier.value} model returned empty content")
    return text


def _messages(system: str, user: str | list[BaseMessage]) -> list[BaseMessage]:
    head: list[BaseMessage] = [SystemMessage(system)]
    return head + ([HumanMessage(user)] if isinstance(user, str) else list(user))


async def structured(
    schema: type[T], system: str, user: str | list[BaseMessage], tier: Tier = Tier.FAST
) -> T:
    """Return a validated `schema` instance or raise LLMError.

    1) native tool-calling structured output; 2) up to two JSON-mode attempts validated locally.
    """
    messages = _messages(system, user)
    cfg = run_config(f"structured:{schema.__name__}")

    try:
        runnable = chat_model(tier, 0.1).with_structured_output(schema, method="function_calling")
        result = await runnable.ainvoke(messages, config=cfg)
        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            return schema.model_validate(result)
    except Exception as exc:  # noqa: BLE001 - fall through to JSON mode
        log.debug("llm.structured.tool_mode_failed", schema=schema.__name__, error=type(exc).__name__)

    hint = HumanMessage(
        "Respond with ONLY a JSON object matching this JSON schema, no prose:\n"
        + json.dumps(schema.model_json_schema())
    )
    last: Exception | None = None
    for _ in range(2):
        try:
            raw = await chat_model(tier, 0.1).ainvoke(messages + [hint], config=cfg)
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
        content = _text_of(raw.content)
        match = _JSON_OBJECT.search(content)
        try:
            return schema.model_validate_json(match.group(0) if match else content)
        except ValidationError as exc:
            last = exc
    raise LLMError(f"could not get a valid {schema.__name__}: {type(last).__name__ if last else 'unknown'}")
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/llm/test_models.py -v`
Expected: `7 passed`

- [ ] **Step 7: Commit**

```bash
git add src/mavis/llm tests/fakes tests/llm tests/conftest.py
git commit -m "feat(llm): tiered model routing, complete(), structured() with JSON fallback; FakeLLM test double"
```

---

### Task 7: Channel port, FakeChannel / ConsoleChannel, Telegram channel, and the `channel` fixture

**Files:**
- Create: `src/mavis/channels/base.py`, `src/mavis/channels/text.py`, `src/mavis/channels/fake.py`, `src/mavis/channels/telegram.py`
- Modify: `src/mavis/channels/__init__.py`
- Modify: `tests/conftest.py` (append `channel`)
- Create: `tests/channels/__init__.py`, `tests/channels/test_text.py`, `tests/channels/test_telegram.py`, `tests/channels/test_fake.py`

**Interfaces:**
- Consumes: `Button`, `get_settings()`
- Produces:
  - `mavis.channels.base`: `Channel` (verbatim from the index), `ChannelRateLimited(retry_after: float)`
  - `mavis.channels.text`: `TELEGRAM_LIMIT = 4096`, `split_text(text, limit=4096) -> list[str]`
  - `mavis.channels.fake`: `SentItem`, `FakeChannel` (`.sent`, `.texts`, `.fail_next: list[Exception]`), `ConsoleChannel`
  - `mavis.channels.telegram`: `TelegramChannel(token, bot=None)`
  - `mavis.channels`: `get_channel() -> Channel`, `set_channel(channel | None)`
  - fixture `channel` (a `FakeChannel` installed via `set_channel`)

- [ ] **Step 1: Append the `channel` fixture to `tests/conftest.py`**

```python
@pytest.fixture
def channel():
    """A FakeChannel installed as the process channel; inspect `.sent` / `.texts`."""
    from mavis.channels import set_channel
    from mavis.channels.fake import FakeChannel

    ch = FakeChannel()
    set_channel(ch)
    yield ch
    set_channel(None)
```

- [ ] **Step 2: Write the failing tests**

`tests/channels/__init__.py`: empty file.

`tests/channels/test_text.py`:

```python
from mavis.channels.text import split_text


def test_short_text_is_one_chunk() -> None:
    assert split_text("  hello  ") == ["hello"]


def test_empty_text_is_no_chunks() -> None:
    assert split_text("   ") == []


def test_split_long_text_prefers_newlines() -> None:
    para = "a" * 3000
    chunks = split_text(f"{para}\n{para}")
    assert chunks == [para, para]
    assert all(len(c) <= 4096 for c in chunks)


def test_split_without_whitespace_hard_cuts() -> None:
    chunks = split_text("x" * 9000)
    assert [len(c) for c in chunks] == [4096, 4096, 808]
```

`tests/channels/test_fake.py`:

```python
from mavis.channels.fake import FakeChannel
from mavis.domain.messages import Button


async def test_fake_channel_records_everything(tmp_path) -> None:
    ch = FakeChannel()
    ids = await ch.send_text(1, "hello", buttons=[[Button(label="OK", data="ok")]])
    await ch.send_typing(1)
    doc = tmp_path / "a.txt"
    doc.write_text("x")
    await ch.send_document(1, str(doc), caption="here")
    dest = await ch.download_file("f1", str(tmp_path / "dl.bin"))
    assert ids == [1]
    assert ch.texts == ["hello"]
    assert ch.sent[0].buttons[0][0].data == "ok"
    assert [s.kind for s in ch.sent] == ["text", "typing", "document"]
    assert (tmp_path / "dl.bin").read_bytes() == b"fake-file" and dest.endswith("dl.bin")


async def test_fake_channel_can_fail_on_demand() -> None:
    ch = FakeChannel()
    ch.fail_next.append(RuntimeError("down"))
    try:
        await ch.send_text(1, "x")
    except RuntimeError:
        pass
    assert ch.texts == []
    await ch.send_text(1, "y")
    assert ch.texts == ["y"]
```

`tests/channels/test_telegram.py`:

```python
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

from mavis.channels.base import ChannelRateLimited
from mavis.channels.telegram import TelegramChannel
from mavis.domain.messages import Button


class StubBot:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.raise_on_send: Exception | None = None
        self._n = 0

    async def initialize(self) -> None:
        self.calls.append(("initialize",))

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.raise_on_send:
            raise self.raise_on_send
        self._n += 1
        self.calls.append(("message", chat_id, text, reply_markup))
        return SimpleNamespace(message_id=self._n)

    async def send_chat_action(self, chat_id, action):
        self.calls.append(("action", chat_id, action))

    async def send_document(self, chat_id, document, filename=None, caption=None):
        self.calls.append(("document", chat_id, filename, caption))
        return SimpleNamespace(message_id=99)


async def test_long_text_split_with_buttons_on_last_chunk() -> None:
    bot = StubBot()
    ch = TelegramChannel("token", bot=bot)
    ids = await ch.send_text(5, ("a" * 3000) + "\n" + ("b" * 3000), buttons=[[Button(label="OK", data="ok")]])
    messages = [c for c in bot.calls if c[0] == "message"]
    assert ids == [1, 2]
    assert messages[0][3] is None
    assert messages[1][3].inline_keyboard[0][0].callback_data == "ok"
    assert bot.calls[0] == ("initialize",)


async def test_retry_after_maps_to_channel_rate_limited() -> None:
    bot = StubBot()
    bot.raise_on_send = RetryAfter(5)
    with pytest.raises(ChannelRateLimited) as info:
        await TelegramChannel("token", bot=bot).send_text(5, "hi")
    assert info.value.retry_after == 5.0


async def test_send_document_and_typing(tmp_path) -> None:
    bot = StubBot()
    ch = TelegramChannel("token", bot=bot)
    f = tmp_path / "deck.pptx"
    f.write_bytes(b"x")
    assert await ch.send_document(5, str(f), caption="Your deck") == 99
    await ch.send_typing(5)
    assert ("document", 5, "deck.pptx", "Your deck") in bot.calls
    assert any(c[0] == "action" for c in bot.calls)


def test_get_channel_defaults_to_console_without_token(settings) -> None:
    from mavis.channels import get_channel, set_channel
    from mavis.channels.fake import ConsoleChannel

    set_channel(None)
    try:
        assert isinstance(get_channel(), ConsoleChannel)
    finally:
        set_channel(None)
```

- [ ] **Step 3: Run them to confirm they fail**

Run: `uv run pytest tests/channels -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.channels.text'`

- [ ] **Step 4: Implement `src/mavis/channels/base.py`**

The `Channel` protocol is verbatim from the index. `ChannelRateLimited` is a contract addition.

```python
from __future__ import annotations

from typing import Protocol

from mavis.domain.messages import Button


class ChannelRateLimited(Exception):
    """The provider asked us to slow down; retry after `retry_after` seconds (not a delivery failure)."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class Channel(Protocol):
    async def send_text(self, chat_id: int, text: str, buttons: list[list[Button]] | None = None) -> list[int]:
        """Send text (split at 4096 chars). Returns provider message ids."""

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int: ...

    async def send_typing(self, chat_id: int) -> None: ...

    async def download_file(self, file_id: str, dest_path: str) -> str: ...
```

- [ ] **Step 5: Implement `src/mavis/channels/text.py`**

```python
from __future__ import annotations

TELEGRAM_LIMIT = 4096


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on newlines, then spaces, then hard-cut, so each chunk fits the provider limit."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks
```

- [ ] **Step 6: Implement `src/mavis/channels/fake.py`**

```python
"""In-memory channels: FakeChannel for tests, ConsoleChannel for `mavis chat` and token-less dev."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mavis.channels.text import split_text
from mavis.config import get_settings
from mavis.domain.messages import Button


@dataclass
class SentItem:
    kind: Literal["text", "document", "typing"]
    chat_id: int
    text: str = ""
    buttons: list[list[Button]] = field(default_factory=list)
    path: str | None = None


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[SentItem] = []
        self.fail_next: list[Exception] = []
        self._next_id = 1

    @property
    def texts(self) -> list[str]:
        return [s.text for s in self.sent if s.kind == "text"]

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    async def send_text(self, chat_id: int, text: str, buttons: list[list[Button]] | None = None) -> list[int]:
        self._maybe_fail()
        chunks = split_text(text)
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            self.sent.append(SentItem("text", chat_id, chunk, (buttons or []) if last else []))
            ids.append(self._id())
        return ids

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int:
        self._maybe_fail()
        self.sent.append(SentItem("document", chat_id, caption, path=path))
        return self._id()

    async def send_typing(self, chat_id: int) -> None:
        self.sent.append(SentItem("typing", chat_id))

    async def download_file(self, file_id: str, dest_path: str) -> str:
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"fake-file")
        return dest_path


class ConsoleChannel(FakeChannel):
    """Prints what the agent says. Used by `mavis chat` and by `mavis dev` without a bot token."""

    async def send_text(self, chat_id: int, text: str, buttons: list[list[Button]] | None = None) -> list[int]:
        ids = await super().send_text(chat_id, text, buttons)
        name = get_settings().agent_name
        print(f"\n{name}: {text}")
        if buttons:
            print("   " + "  ".join(f"[{b.label} → {b.data}]" for row in buttons for b in row))
        return ids

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int:
        msg_id = await super().send_document(chat_id, path, caption)
        print(f"\n{get_settings().agent_name}: 📎 {path} {caption}")
        return msg_id
```

- [ ] **Step 7: Implement `src/mavis/channels/telegram.py`**

```python
"""Telegram adapter over python-telegram-bot's Bot. Only this module imports `telegram`."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import RetryAfter

from mavis.channels.base import ChannelRateLimited
from mavis.channels.text import split_text
from mavis.domain.messages import Button

log = structlog.get_logger(__name__)


def _seconds(value: int | float | timedelta) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


class TelegramChannel:
    def __init__(self, token: str, bot: Any | None = None) -> None:
        self._bot = bot or Bot(token)
        self._ready = False

    async def _ensure(self) -> None:
        if not self._ready:
            await self._bot.initialize()
            self._ready = True

    @staticmethod
    def _markup(buttons: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
        if not buttons:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(b.label, callback_data=b.data) for b in row] for row in buttons]
        )

    async def send_text(self, chat_id: int, text: str, buttons: list[list[Button]] | None = None) -> list[int]:
        await self._ensure()
        chunks = split_text(text)
        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            markup = self._markup(buttons) if i == len(chunks) - 1 else None
            try:
                msg = await self._bot.send_message(chat_id=chat_id, text=chunk, reply_markup=markup)
            except RetryAfter as exc:
                raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
            ids.append(msg.message_id)
        return ids

    async def send_document(self, chat_id: int, path: str, caption: str = "") -> int:
        await self._ensure()
        try:
            with open(path, "rb") as fh:
                msg = await self._bot.send_document(
                    chat_id=chat_id, document=fh, filename=Path(path).name, caption=caption[:1024] or None
                )
        except RetryAfter as exc:
            raise ChannelRateLimited(_seconds(exc.retry_after)) from exc
        return msg.message_id

    async def send_typing(self, chat_id: int) -> None:
        await self._ensure()
        try:
            await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as exc:  # noqa: BLE001 - typing is cosmetic
            log.debug("telegram.typing_failed", error=type(exc).__name__)

    async def download_file(self, file_id: str, dest_path: str) -> str:
        await self._ensure()
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        tg_file = await self._bot.get_file(file_id)
        await tg_file.download_to_drive(dest_path)
        return dest_path
```

- [ ] **Step 8: Implement `src/mavis/channels/__init__.py`**

```python
"""Process-wide channel accessor. Business logic depends on the Channel protocol only."""

from __future__ import annotations

from mavis.channels.base import Channel
from mavis.config import get_settings

_channel: Channel | None = None


def get_channel() -> Channel:
    global _channel
    if _channel is None:
        token = get_settings().telegram_bot_token
        if token:
            from mavis.channels.telegram import TelegramChannel

            _channel = TelegramChannel(token)
        else:
            from mavis.channels.fake import ConsoleChannel

            _channel = ConsoleChannel()
    return _channel


def set_channel(channel: Channel | None) -> None:
    global _channel
    _channel = channel
```

- [ ] **Step 9: Run the tests**

Run: `uv run pytest tests/channels -v`
Expected: `10 passed`

- [ ] **Step 10: Commit**

```bash
git add src/mavis/channels tests/channels tests/conftest.py
git commit -m "feat(channels): Channel port, Telegram adapter with splitting/rate limits, fake and console channels"
```

---

### Task 8: Event bus (in-process + Redis Streams) and the `bus` fixture

**Files:**
- Create: `src/mavis/bus/base.py`, `src/mavis/bus/inprocess.py`, `src/mavis/bus/redis_streams.py`
- Modify: `src/mavis/bus/__init__.py`
- Modify: `tests/conftest.py` (append `bus`)
- Create: `tests/bus/__init__.py`, `tests/bus/test_inprocess.py`, `tests/bus/test_redis_streams.py`

**Interfaces:**
- Consumes: `Event`, `Job`, `get_settings().redis_url`
- Produces:
  - `mavis.bus.base`: `Stream`, `EventHandler`, `JobHandler`, `EventBus` (verbatim from the index)
  - `InProcessBus` (`MAX_ATTEMPTS = 5`, `wait_idle()`, `dead_events`, `dead_jobs`)
  - `RedisStreamsBus(client, *, claim_idle_ms=60_000, block_ms=5_000, dedupe_ttl_s=604_800, max_attempts=5)`
  - `mavis.bus`: `get_bus() -> EventBus`, `set_bus(bus | None)`, `get_redis() -> Redis | None`
  - fixture `bus` (an `InProcessBus` installed via `set_bus`)

- [ ] **Step 1: Append the `bus` fixture to `tests/conftest.py`**

```python
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
```

- [ ] **Step 2: Write the failing tests**

`tests/bus/__init__.py`: empty file.

`tests/bus/test_inprocess.py`:

```python
import asyncio
from datetime import UTC, datetime

from mavis.bus.inprocess import InProcessBus
from mavis.domain.events import Event, EventType, Job, JobKind


def ev(event_id: str, user_id: int = 1) -> Event:
    return Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=datetime.now(UTC), source="test")


async def test_duplicate_event_processed_once() -> None:
    bus = InProcessBus()
    seen: list[str] = []

    async def handler(e: Event) -> None:
        seen.append(e.id)

    assert await bus.publish(ev("tg:update:1"))
    assert not await bus.publish(ev("tg:update:1"))
    task = asyncio.create_task(bus.consume_events("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert seen == ["tg:update:1"]


async def test_failing_event_retried_then_dead_lettered() -> None:
    bus = InProcessBus()
    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    await bus.publish(ev("e1"))
    task = asyncio.create_task(bus.consume_events("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert calls == InProcessBus.MAX_ATTEMPTS
    assert [e.id for e in bus.dead_events] == ["e1"]


async def test_event_succeeds_on_retry() -> None:
    bus = InProcessBus()
    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("flaky")

    await bus.publish(ev("e1"))
    task = asyncio.create_task(bus.consume_events("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert calls == 3 and bus.dead_events == []


async def test_jobs_delivered_and_attempts_incremented_on_retry() -> None:
    bus = InProcessBus()
    attempts: list[int] = []

    async def handler(j: Job) -> None:
        attempts.append(j.attempts)
        if j.attempts == 0:
            raise RuntimeError("first try fails")

    await bus.enqueue(Job(id="j1", user_id=1, kind=JobKind.LEARN))
    task = asyncio.create_task(bus.consume_jobs("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert attempts == [0, 1]
```

`tests/bus/test_redis_streams.py`:

```python
import asyncio
from datetime import UTC, datetime

import pytest
from fakeredis import FakeAsyncRedis

from tests.fakes import wait_until
from mavis.bus.base import Stream
from mavis.bus.redis_streams import RedisStreamsBus
from mavis.domain.events import Event, EventType, Job, JobKind


def ev(event_id: str) -> Event:
    return Event(id=event_id, user_id=1, type=EventType.USER_MESSAGE, occurred_at=datetime.now(UTC), source="t")


@pytest.fixture
async def rbus():
    client = FakeAsyncRedis(decode_responses=True)
    bus = RedisStreamsBus(client, claim_idle_ms=0, block_ms=10)
    yield bus, client
    await bus.close()


async def test_publish_dedupes(rbus) -> None:
    bus, client = rbus
    assert await bus.publish(ev("tg:update:7"))
    assert not await bus.publish(ev("tg:update:7"))
    assert await client.xlen(Stream.EVENTS.value) == 1
    assert 0 < await client.ttl("mavis:seen:tg:update:7") <= 7 * 24 * 3600


async def test_consume_delivers_and_acks(rbus) -> None:
    bus, client = rbus
    got: list[Event] = []

    async def handler(e: Event) -> None:
        got.append(e)

    await bus.publish(ev("e1"))
    task = asyncio.create_task(bus.consume_events("workers", "w1", handler))
    await wait_until(lambda: len(got) == 1)
    await wait_until(lambda: _pending_count(client, Stream.EVENTS.value, "workers"))
    task.cancel()
    assert got[0].id == "e1" and got[0].type is EventType.USER_MESSAGE


async def _pending_count(client, stream: str, group: str) -> bool:
    info = await client.xpending(stream, group)
    return info["pending"] == 0


async def test_failing_message_goes_to_dlq_after_max_attempts(rbus) -> None:
    bus, client = rbus
    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    await bus.publish(ev("bad"))
    task = asyncio.create_task(bus.consume_events("workers", "w1", handler))

    async def dead() -> bool:
        return await client.xlen(f"{Stream.EVENTS.value}:dlq") == 1

    await wait_until(dead, timeout=5)
    task.cancel()
    assert calls == 5


async def test_jobs_round_trip(rbus) -> None:
    bus, _ = rbus
    got: list[Job] = []

    async def handler(j: Job) -> None:
        got.append(j)

    await bus.enqueue(Job(id="j1", user_id=1, kind=JobKind.LEARN, payload={"x": 1}))
    task = asyncio.create_task(bus.consume_jobs("workers", "w1", handler))
    await wait_until(lambda: len(got) == 1)
    task.cancel()
    assert got[0].payload == {"x": 1}


def test_get_bus_selects_in_process_without_redis(settings) -> None:
    from mavis.bus import get_bus, get_redis, set_bus
    from mavis.bus.inprocess import InProcessBus

    set_bus(None)
    try:
        assert isinstance(get_bus(), InProcessBus)
        assert get_redis() is None
    finally:
        set_bus(None)
```

- [ ] **Step 3: Run them to confirm they fail**

Run: `uv run pytest tests/bus -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.bus.inprocess'`

- [ ] **Step 4: Create `src/mavis/bus/base.py` verbatim from the index**

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from mavis.domain.events import Event, Job


class Stream(StrEnum):
    EVENTS = "mavis:events"
    JOBS = "mavis:jobs"


EventHandler = Callable[[Event], Awaitable[None]]
JobHandler = Callable[[Job], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, event: Event) -> bool:
        """Publish an event. Returns False if this event id was already seen (deduped)."""

    async def enqueue(self, job: Job) -> None: ...

    async def consume_events(self, group: str, consumer: str, handler: EventHandler) -> None:
        """Run forever, delivering events to handler; ack on success, retry ≤5 then DLQ."""

    async def consume_jobs(self, group: str, consumer: str, handler: JobHandler) -> None: ...

    async def close(self) -> None: ...
```

- [ ] **Step 5: Implement `src/mavis/bus/inprocess.py`**

```python
"""Single-process bus for dev/tests: asyncio queues, in-memory dedupe, retry ≤5 then dead-letter list."""

from __future__ import annotations

import asyncio

import structlog

from mavis.bus.base import EventHandler, JobHandler
from mavis.domain.events import Event, Job

log = structlog.get_logger(__name__)


class InProcessBus:
    MAX_ATTEMPTS = 5

    def __init__(self) -> None:
        self._events: asyncio.Queue[tuple[Event, int]] = asyncio.Queue()
        self._jobs: asyncio.Queue[Job] = asyncio.Queue()
        self._seen: set[str] = set()
        self.dead_events: list[Event] = []
        self.dead_jobs: list[Job] = []
        self._closed = False

    async def publish(self, event: Event) -> bool:
        if event.id in self._seen:
            return False
        self._seen.add(event.id)
        await self._events.put((event, 0))
        return True

    async def enqueue(self, job: Job) -> None:
        await self._jobs.put(job)

    async def consume_events(self, group: str, consumer: str, handler: EventHandler) -> None:
        while not self._closed:
            event, attempts = await self._events.get()
            try:
                await handler(event)
            except Exception:
                log.exception("bus.event_failed", event_id=event.id, attempt=attempts + 1)
                if attempts + 1 >= self.MAX_ATTEMPTS:
                    self.dead_events.append(event)
                else:
                    self._events.put_nowait((event, attempts + 1))
            finally:
                self._events.task_done()

    async def consume_jobs(self, group: str, consumer: str, handler: JobHandler) -> None:
        while not self._closed:
            job = await self._jobs.get()
            try:
                await handler(job)
            except Exception:
                log.exception("bus.job_failed", job_id=job.id, kind=job.kind, attempt=job.attempts + 1)
                if job.attempts + 1 >= self.MAX_ATTEMPTS:
                    self.dead_jobs.append(job)
                else:
                    self._jobs.put_nowait(job.model_copy(update={"attempts": job.attempts + 1}))
            finally:
                self._jobs.task_done()

    async def wait_idle(self) -> None:
        """Block until both queues are drained, including work that publishes more work."""
        while True:
            await self._events.join()
            await self._jobs.join()
            if self._events.empty() and self._jobs.empty():
                return

    async def close(self) -> None:
        self._closed = True
```

- [ ] **Step 6: Implement `src/mavis/bus/redis_streams.py`**

```python
"""Redis Streams bus: consumer groups, SET NX dedupe (7d), XAUTOCLAIM for stuck messages, DLQ stream."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from mavis.bus.base import EventHandler, JobHandler, Stream
from mavis.domain.events import Event, Job

log = structlog.get_logger(__name__)
MAXLEN = 100_000


class RedisStreamsBus:
    def __init__(
        self,
        client: Redis,
        *,
        claim_idle_ms: int = 60_000,
        block_ms: int = 5_000,
        dedupe_ttl_s: int = 7 * 24 * 3600,
        max_attempts: int = 5,
    ) -> None:
        self._r = client
        self._claim_idle_ms = claim_idle_ms
        self._block_ms = block_ms
        self._ttl = dedupe_ttl_s
        self._max = max_attempts
        self._closed = False

    async def publish(self, event: Event) -> bool:
        fresh = await self._r.set(f"mavis:seen:{event.id}", "1", nx=True, ex=self._ttl)
        if not fresh:
            return False
        await self._r.xadd(Stream.EVENTS.value, {"data": event.model_dump_json()}, maxlen=MAXLEN,
                           approximate=True)
        return True

    async def enqueue(self, job: Job) -> None:
        await self._r.xadd(Stream.JOBS.value, {"data": job.model_dump_json()}, maxlen=MAXLEN, approximate=True)

    async def consume_events(self, group: str, consumer: str, handler: EventHandler) -> None:
        async def handle(data: str, attempts: int) -> None:
            await handler(Event.model_validate_json(data))

        await self._consume(Stream.EVENTS, group, consumer, handle)

    async def consume_jobs(self, group: str, consumer: str, handler: JobHandler) -> None:
        async def handle(data: str, attempts: int) -> None:
            job = Job.model_validate_json(data)
            await handler(job.model_copy(update={"attempts": attempts}))

        await self._consume(Stream.JOBS, group, consumer, handle)

    async def close(self) -> None:
        self._closed = True
        await self._r.aclose()

    # --- internals -------------------------------------------------------------
    async def _ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._r.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume(
        self, stream: Stream, group: str, consumer: str, handle: Callable[[str, int], Awaitable[None]]
    ) -> None:
        await self._ensure_group(stream.value, group)
        while not self._closed:
            try:
                entries = await self._claim_stale(stream.value, group, consumer)
                if not entries:
                    resp = await self._r.xreadgroup(group, consumer, {stream.value: ">"}, count=10,
                                                    block=self._block_ms)
                    entries = [entry for _name, items in (resp or []) for entry in items]
                for msg_id, fields in entries:
                    await self._process(stream.value, group, msg_id, fields, handle)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._closed:
                    return
                log.exception("bus.consume_loop_error", stream=stream.value)
                await asyncio.sleep(1)

    async def _claim_stale(self, stream: str, group: str, consumer: str) -> list[tuple[str, dict[str, Any]]]:
        res = await self._r.xautoclaim(stream, group, consumer, min_idle_time=self._claim_idle_ms,
                                       start_id="0-0", count=10)
        messages = res[1] if len(res) > 1 else []
        return [(mid, fields) for mid, fields in messages if fields]

    async def _process(
        self, stream: str, group: str, msg_id: str, fields: dict[str, Any],
        handle: Callable[[str, int], Awaitable[None]],
    ) -> None:
        data = fields.get("data") or fields.get(b"data")
        if isinstance(data, bytes):
            data = data.decode()
        attempts_key = f"mavis:attempts:{stream}"
        prior = int(await self._r.hget(attempts_key, msg_id) or 0)
        try:
            await handle(data, prior)
        except Exception:
            attempt = await self._r.hincrby(attempts_key, msg_id, 1)
            log.exception("bus.handler_failed", stream=stream, msg_id=str(msg_id), attempt=attempt)
            if attempt >= self._max:
                await self._r.xadd(f"{stream}:dlq", {"data": data, "msg_id": str(msg_id)})
                await self._r.xack(stream, group, msg_id)
                await self._r.hdel(attempts_key, msg_id)
            return  # unacked: XAUTOCLAIM redelivers after claim_idle_ms
        await self._r.xack(stream, group, msg_id)
        await self._r.hdel(attempts_key, msg_id)
```

- [ ] **Step 7: Implement `src/mavis/bus/__init__.py`**

```python
"""Process-wide bus accessor: Redis Streams when REDIS_URL is set, otherwise in-process."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mavis.bus.base import EventBus
from mavis.config import get_settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

_bus: EventBus | None = None
_redis: Redis | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        client = get_redis()
        if client is not None:
            from redis.asyncio import Redis

            from mavis.bus.redis_streams import RedisStreamsBus

            # the bus owns its own connection pool; get_redis() stays available for locks/health
            _bus = RedisStreamsBus(Redis.from_url(get_settings().redis_url, decode_responses=True))
        else:
            from mavis.bus.inprocess import InProcessBus

            _bus = InProcessBus()
    return _bus


def set_bus(bus: EventBus | None) -> None:
    global _bus
    _bus = bus


def get_redis() -> Redis | None:
    """Shared Redis client for locks and health checks, or None in single-process mode."""
    global _redis
    url = get_settings().redis_url
    if not url:
        return None
    if _redis is None:
        from redis.asyncio import Redis

        _redis = Redis.from_url(url, decode_responses=True)
    return _redis
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/bus -v`
Expected: `9 passed`

- [ ] **Step 9: Commit**

```bash
git add src/mavis/bus tests/bus tests/conftest.py
git commit -m "feat(bus): EventBus port with in-process and Redis Streams implementations (dedupe, retry, DLQ)"
```

---

### Task 9: Outbox sender

**Files:**
- Create: `src/mavis/channels/outbox_sender.py`
- Create: `tests/channels/test_outbox_sender.py`

**Interfaces:**
- Consumes: `outbox` repo (Task 4), `users.get`, `Channel`, `ChannelRateLimited`, `get_channel()`
- Produces: `OutboxSender(channel: Channel | None = None)` with `run_once(now=None, limit=20) -> int` (count delivered) and `run_forever(interval_s=0.3)`; module function `deliver_pending(channel=None, limit=50) -> int`; `MAX_ATTEMPTS = 8`; backoff `min(2**attempts, 300)` seconds

- [ ] **Step 1: Write the failing tests**

`tests/channels/test_outbox_sender.py`:

```python
from datetime import timedelta

from mavis.channels.base import ChannelRateLimited
from mavis.channels.outbox_sender import MAX_ATTEMPTS, OutboxSender
from mavis.domain.messages import Button, Outbound
from mavis.store.db import Session, utcnow
from mavis.store.models import OutboxMessage
from mavis.store.repo import outbox, users


async def _user() -> int:
    u, _ = await users.get_or_create_by_chat(555, "Jai")
    return u.id


async def _row(outbox_id: int) -> OutboxMessage:
    async with Session() as s:
        return await s.get_one(OutboxMessage, outbox_id)


async def test_delivers_text_with_buttons_and_marks_sent(db, channel) -> None:
    uid = await _user()
    oid = await outbox.enqueue_now(Outbound(user_id=uid, text="Send it?",
                                            buttons=[[Button(label="✅", data="a:1:y")]]))
    assert await OutboxSender(channel).run_once() == 1
    assert channel.texts == ["Send it?"]
    assert channel.sent[0].chat_id == 555 and channel.sent[0].buttons[0][0].data == "a:1:y"
    row = await _row(oid)
    assert row.status == "sent" and row.provider_message_ids == [1]
    assert await OutboxSender(channel).run_once() == 0  # nothing left


async def test_deliver_pending_helper(db, channel) -> None:
    from mavis.channels.outbox_sender import deliver_pending

    uid = await _user()
    await outbox.enqueue_now(Outbound(user_id=uid, text="one"))
    await outbox.enqueue_now(Outbound(user_id=uid, text="two"))
    assert await deliver_pending(channel) == 2
    assert channel.texts == ["one", "two"]


async def test_delivers_document_with_caption(db, channel, tmp_path) -> None:
    uid = await _user()
    f = tmp_path / "deck.pptx"
    f.write_bytes(b"x")
    await outbox.enqueue_now(Outbound(user_id=uid, text="Your deck", document_path=str(f)))
    await OutboxSender(channel).run_once()
    assert channel.sent[0].kind == "document" and channel.sent[0].path == str(f)


async def test_failure_backs_off_then_succeeds(db, channel) -> None:
    uid = await _user()
    oid = await outbox.enqueue_now(Outbound(user_id=uid, text="hi"))
    channel.fail_next.append(RuntimeError("network"))
    now = utcnow()
    assert await OutboxSender(channel).run_once(now) == 0
    row = await _row(oid)
    assert row.status == "pending" and row.attempts == 1
    assert row.next_attempt_at >= now + timedelta(seconds=2) - timedelta(milliseconds=5)
    assert await OutboxSender(channel).run_once(now) == 0  # not due yet
    assert await OutboxSender(channel).run_once(now + timedelta(seconds=3)) == 1
    assert channel.texts == ["hi"]


async def test_rate_limit_defers_without_counting_attempt(db, channel) -> None:
    uid = await _user()
    oid = await outbox.enqueue_now(Outbound(user_id=uid, text="hi"))
    channel.fail_next.append(ChannelRateLimited(7))
    now = utcnow()
    await OutboxSender(channel).run_once(now)
    row = await _row(oid)
    assert row.attempts == 0 and row.status == "pending"
    assert abs((row.next_attempt_at - (now + timedelta(seconds=7))).total_seconds()) < 1


async def test_gives_up_after_max_attempts(db, channel) -> None:
    uid = await _user()
    oid = await outbox.enqueue_now(Outbound(user_id=uid, text="hi"))
    now = utcnow()
    for i in range(MAX_ATTEMPTS):
        channel.fail_next.append(RuntimeError("down"))
        await OutboxSender(channel).run_once(now + timedelta(hours=i))
    row = await _row(oid)
    assert row.status == "failed" and channel.texts == []


async def test_deduped_messages_sent_once(db, channel) -> None:
    uid = await _user()
    await outbox.enqueue_now(Outbound(user_id=uid, text="hi", dedupe_key="reply:e1:0"))
    await outbox.enqueue_now(Outbound(user_id=uid, text="hi", dedupe_key="reply:e1:0"))
    await OutboxSender(channel).run_once()
    assert channel.texts == ["hi"]
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `uv run pytest tests/channels/test_outbox_sender.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.channels.outbox_sender'`

- [ ] **Step 3: Implement `src/mavis/channels/outbox_sender.py`**

```python
"""Delivers outbox rows through the Channel with leasing, exponential backoff and rate-limit handling."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import structlog

from mavis.channels import get_channel
from mavis.channels.base import Channel, ChannelRateLimited
from mavis.store.db import utcnow
from mavis.store.models import OutboxMessage
from mavis.store.repo import outbox, users

log = structlog.get_logger(__name__)
MAX_ATTEMPTS = 8


class OutboxSender:
    def __init__(self, channel: Channel | None = None) -> None:
        self._channel = channel

    @property
    def channel(self) -> Channel:
        return self._channel or get_channel()

    async def run_once(self, now: datetime | None = None, limit: int = 20) -> int:
        now = now or utcnow()
        delivered = 0
        for row in await outbox.due(now, limit):
            if not await outbox.claim(row.id, now):
                continue  # another sender owns it
            try:
                await self._deliver(row)
            except ChannelRateLimited as exc:
                await outbox.mark_retry(row.id, "rate limited", now + timedelta(seconds=exc.retry_after),
                                        count_attempt=False)
            except Exception as exc:  # noqa: BLE001
                attempts = row.attempts + 1
                log.warning("outbox.delivery_failed", outbox_id=row.id, attempt=attempts, error=repr(exc))
                if attempts >= MAX_ATTEMPTS:
                    await outbox.mark_failed(row.id, repr(exc)[:500])
                else:
                    await outbox.mark_retry(row.id, repr(exc)[:500],
                                            now + timedelta(seconds=min(2**attempts, 300)))
            else:
                delivered += 1
        return delivered

    async def _deliver(self, row: OutboxMessage) -> None:
        user = await users.get(row.user_id)
        if user.telegram_chat_id is None:
            raise RuntimeError(f"user {row.user_id} has no chat id")
        msg = outbox.to_outbound(row)
        ids: list[int] = []
        if msg.document_path:
            ids.append(await self.channel.send_document(user.telegram_chat_id, msg.document_path, msg.text))
        elif msg.text:
            ids += await self.channel.send_text(user.telegram_chat_id, msg.text, msg.buttons or None)
        await outbox.mark_sent(row.id, ids)

    async def run_forever(self, interval_s: float = 0.3) -> None:
        while True:
            try:
                delivered = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox.loop_error")
                delivered = 0
            if delivered == 0:
                await asyncio.sleep(interval_s)


async def deliver_pending(channel: Channel | None = None, limit: int = 50) -> int:
    """Deliver everything due right now (used by tests and the local chat REPL)."""
    return await OutboxSender(channel).run_once(limit=limit)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/channels/test_outbox_sender.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add src/mavis/channels/outbox_sender.py tests/channels/test_outbox_sender.py
git commit -m "feat(channels): outbox sender with leasing, backoff and rate-limit deferral"
```

---

### Task 10: Worker (locks, handler registry, LLM fallback)

**Files:**
- Create: `src/mavis/worker/locks.py`, `src/mavis/worker/runner.py`
- Modify: `tests/conftest.py` (append an autouse registry reset)
- Create: `tests/worker/__init__.py`, `tests/worker/test_runner.py`

**Interfaces:**
- Consumes: `get_redis()`, `EventBus`, `Event`, `Job`, `LLMError`, `Trust`, `Outbound`, `outbox.enqueue_now`
- Produces:
  - `mavis.worker.locks.user_lock(user_id, timeout_s=300)` (async context manager)
  - `mavis.worker.runner`: `register_event_handler(event_type, fn, *, replace=False)`, `register_job_handler(kind, fn)`, `clear_handlers()`, `handle_event(event)`, `handle_job(job)`, `run_worker(bus, consumer)`, `FALLBACK_TEXT`, `WORKER_GROUP = "workers"`

- [ ] **Step 1: Append the autouse registry reset to `tests/conftest.py`**

```python
@pytest.fixture(autouse=True)
def _reset_worker_registry():
    """Handlers registered by one test must not leak into the next."""
    yield
    from mavis.worker.runner import clear_handlers

    clear_handlers()
```

- [ ] **Step 2: Write the failing tests**

`tests/worker/__init__.py`: empty file.

`tests/worker/test_runner.py`:

```python
import asyncio
from datetime import UTC, datetime

import pytest

from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.store.db import utcnow
from mavis.store.repo import outbox, users
from mavis.worker.runner import (
    FALLBACK_TEXT, handle_event, handle_job, register_event_handler, register_job_handler, run_worker,
)


def ev(event_id: str, user_id: int = 1, trust: Trust = Trust.USER) -> Event:
    return Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=datetime.now(UTC), source="test", payload={"text": "hi"}, trust=trust)


async def test_turns_serialised_per_user(settings) -> None:
    log: list[str] = []

    async def slow(event: Event) -> None:
        log.append(f"start:{event.id}")
        await asyncio.sleep(0.05)
        log.append(f"end:{event.id}")

    register_event_handler(EventType.USER_MESSAGE, slow)
    await asyncio.gather(handle_event(ev("a", user_id=1)), handle_event(ev("b", user_id=1)))
    assert log == ["start:a", "end:a", "start:b", "end:b"]


async def test_different_users_run_concurrently(settings) -> None:
    log: list[str] = []

    async def slow(event: Event) -> None:
        log.append(f"start:{event.id}")
        await asyncio.sleep(0.05)
        log.append(f"end:{event.id}")

    register_event_handler(EventType.USER_MESSAGE, slow)
    await asyncio.gather(handle_event(ev("a", user_id=1)), handle_event(ev("b", user_id=2)))
    assert log[:2] == ["start:a", "start:b"]


async def test_llm_failure_sends_fallback_message(db) -> None:
    user, _ = await users.get_or_create_by_chat(42, "Jai")

    async def broken(event: Event) -> None:
        raise LLMError("model timeout")

    register_event_handler(EventType.USER_MESSAGE, broken)
    for _ in range(2):  # the bus retries; the fallback must still be sent only once
        with pytest.raises(LLMError):
            await handle_event(ev("tg:update:9", user_id=user.id))
    rows = await outbox.due(utcnow())
    assert [r.text for r in rows] == [FALLBACK_TEXT]


async def test_llm_failure_on_system_event_sends_nothing(db) -> None:
    user, _ = await users.get_or_create_by_chat(42, "Jai")

    async def broken(event: Event) -> None:
        raise LLMError("x")

    register_event_handler(EventType.USER_MESSAGE, broken)
    with pytest.raises(LLMError):
        await handle_event(ev("sys:1", user_id=user.id, trust=Trust.SYSTEM))
    assert await outbox.due(utcnow()) == []


async def test_replace_and_multiple_handlers(settings) -> None:
    calls: list[str] = []

    async def h1(e: Event) -> None:
        calls.append("h1")

    async def h2(e: Event) -> None:
        calls.append("h2")

    register_event_handler(EventType.USER_MESSAGE, h1)
    register_event_handler(EventType.USER_MESSAGE, h1)  # idempotent registration
    register_event_handler(EventType.USER_MESSAGE, h2)
    await handle_event(ev("x"))
    register_event_handler(EventType.USER_MESSAGE, h2, replace=True)
    await handle_event(ev("y"))
    assert calls == ["h1", "h2", "h2"]


async def test_job_dispatch_and_unknown_kind(settings) -> None:
    got: list[Job] = []

    async def learn(job: Job) -> None:
        got.append(job)

    register_job_handler(JobKind.LEARN, learn)
    await handle_job(Job(id="j", user_id=1, kind=JobKind.LEARN))
    await handle_job(Job(id="k", user_id=1, kind=JobKind.CONSOLIDATE))  # no handler: logged, no error
    assert [j.id for j in got] == ["j"]


async def test_run_worker_consumes_bus(bus) -> None:
    got: list[str] = []

    async def h(e: Event) -> None:
        got.append(e.id)

    register_event_handler(EventType.USER_MESSAGE, h)
    task = asyncio.create_task(run_worker(bus, "w1"))
    await bus.publish(ev("e1"))
    await bus.wait_idle()
    task.cancel()
    assert got == ["e1"]
```

- [ ] **Step 3: Run them to confirm they fail**

Run: `uv run pytest tests/worker -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.worker.runner'`

- [ ] **Step 4: Implement `src/mavis/worker/locks.py`**

```python
"""Per-user mutual exclusion: asyncio locks in single-process mode, Redis locks across workers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from weakref import WeakKeyDictionary

from redis.exceptions import LockError

from mavis.bus import get_redis

_local: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = WeakKeyDictionary()


@contextlib.asynccontextmanager
async def user_lock(user_id: int, timeout_s: float = 300) -> AsyncIterator[None]:
    client = get_redis()
    if client is None:
        locks = _local.setdefault(asyncio.get_running_loop(), {})
        async with locks.setdefault(user_id, asyncio.Lock()):
            yield
        return
    lock = client.lock(f"mavis:lock:user:{user_id}", timeout=timeout_s, blocking_timeout=timeout_s)
    if not await lock.acquire():
        raise TimeoutError(f"could not acquire lock for user {user_id}")
    try:
        yield
    finally:
        with contextlib.suppress(LockError):
            await lock.release()
```

- [ ] **Step 5: Implement `src/mavis/worker/runner.py`**

```python
"""Event/job dispatch. Later phases register handlers; events per user are serialised.

Handlers must be idempotent: if any handler for an event raises, the bus retries the event and
every handler for it runs again. Use dedupe keys (outbox) / event_id (messages) / processed_events.
Jobs are NOT run under the user lock; a job handler that mutates conversation state takes it itself.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

import structlog

from mavis.bus.base import EventBus
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.domain.messages import Outbound
from mavis.store.repo import outbox
from mavis.worker.locks import user_lock

log = structlog.get_logger(__name__)

EventFn = Callable[[Event], Awaitable[None]]
JobFn = Callable[[Job], Awaitable[None]]

WORKER_GROUP = "workers"
FALLBACK_TEXT = "Give me a sec, something's slow on my end. I'll get back to you on this."

_event_handlers: dict[EventType, list[EventFn]] = defaultdict(list)
_job_handlers: dict[JobKind, JobFn] = {}


def register_event_handler(event_type: EventType, fn: EventFn, *, replace: bool = False) -> None:
    if replace:
        _event_handlers[event_type] = []
    if fn not in _event_handlers[event_type]:
        _event_handlers[event_type].append(fn)


def register_job_handler(kind: JobKind, fn: JobFn) -> None:
    _job_handlers[kind] = fn


def clear_handlers() -> None:
    _event_handlers.clear()
    _job_handlers.clear()


async def handle_event(event: Event) -> None:
    handlers = list(_event_handlers.get(event.type, []))
    if not handlers:
        log.debug("worker.no_handler", event_type=event.type)
        return
    with structlog.contextvars.bound_contextvars(event_id=event.id, user_id=event.user_id):
        async with user_lock(event.user_id):
            for fn in handlers:
                try:
                    await fn(event)
                except LLMError:
                    if event.trust is Trust.USER:
                        await outbox.enqueue_now(
                            Outbound(user_id=event.user_id, text=FALLBACK_TEXT, dedupe_key=f"fallback:{event.id}")
                        )
                    raise


async def handle_job(job: Job) -> None:
    fn = _job_handlers.get(job.kind)
    if fn is None:
        log.warning("worker.no_job_handler", kind=job.kind)
        return
    with structlog.contextvars.bound_contextvars(job_id=job.id, user_id=job.user_id, kind=job.kind.value):
        await fn(job)


async def run_worker(bus: EventBus, consumer: str) -> None:
    """Consume events and jobs forever (until cancelled)."""
    await asyncio.gather(
        bus.consume_events(WORKER_GROUP, consumer, handle_event),
        bus.consume_jobs(WORKER_GROUP, consumer, handle_job),
    )
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/worker -v`
Expected: `7 passed`

- [ ] **Step 7: Commit**

```bash
git add src/mavis/worker tests/worker tests/conftest.py
git commit -m "feat(worker): per-user serialised event dispatch, handler registry, LLM fallback message"
```

---

### Task 11: Persona and the simple conversation turn

**Files:**
- Create: `src/mavis/agents/persona.py`, `src/mavis/agents/simple_turn.py`, `src/mavis/worker/handlers.py`
- Create: `tests/agents/__init__.py`, `tests/agents/test_persona.py`, `tests/agents/test_simple_turn.py`

**Interfaces:**
- Consumes: `users`, `messages`, `outbox` repos; `llm.complete`; `get_channel()`; `register_event_handler`
- Produces:
  - `mavis.agents.persona`: `system_prompt(user, now: datetime, context: str = "") -> str`, `split_bubbles(text, max_bubbles=3) -> list[str]`, `local_time(user, now) -> datetime`
  - `mavis.agents.simple_turn`: `run_turn(event: Event) -> None`, `HISTORY_LIMIT = 20`, `user_text(event) -> str`
  - `mavis.worker.handlers.register_default_handlers() -> None` (later phases extend or replace it)

- [ ] **Step 1: Write the failing tests**

`tests/agents/__init__.py`: empty file.

`tests/agents/test_persona.py`:

```python
from datetime import UTC, datetime

from mavis.agents.persona import split_bubbles, system_prompt
from mavis.store.repo import users

NOW = datetime(2026, 10, 2, 4, 30, tzinfo=UTC)  # 10:00 in Asia/Kolkata, a Friday


async def test_system_prompt_has_identity_time_and_rules(db) -> None:
    user, _ = await users.get_or_create_by_chat(1, "Jai")
    prompt = system_prompt(user, NOW, context="## Known facts\n- Jawahar is a friend")
    assert "You are Mavis" in prompt
    assert "Friday 02 October 2026, 10:00" in prompt and "Asia/Kolkata" in prompt
    assert "Jai" in prompt
    assert "behind the curtain" in prompt
    assert "waits for their OK" in prompt
    assert prompt.rstrip().endswith("- Jawahar is a friend")


async def test_system_prompt_without_name_asks_for_it(db) -> None:
    user, _ = await users.get_or_create_by_chat(2, None)
    assert "don't know their name yet" in system_prompt(user, NOW)


def test_split_bubbles() -> None:
    assert split_bubbles("Hey!\n\nWhat's up?") == ["Hey!", "What's up?"]
    assert split_bubbles("one\n\n\n\ntwo\n\nthree\n\nfour") == ["one", "two", "three\n\nfour"]
    assert split_bubbles("  single  ") == ["single"]
    assert split_bubbles("   ") == []
```

`tests/agents/test_simple_turn.py`:

```python
from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from mavis.agents.simple_turn import run_turn
from mavis.channels.outbox_sender import OutboxSender
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import messages, outbox, users


def msg_event(user_id: int, text: str, event_id: str = "tg:update:1", **payload) -> Event:
    return Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=datetime.now(UTC),
                 source="telegram", payload={"text": text, **payload}, trust=Trust.USER)


async def test_run_turn_replies_in_bubbles_and_logs(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Hey Jai!\n\nWhat's on your plate today?")
    await run_turn(msg_event(user.id, "hi"))

    assert channel.sent[0].kind == "typing"
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Hey Jai!", "What's on your plate today?"]

    log = await messages.recent(user.id)
    assert [(m.role, m.content) for m in log] == [
        ("user", "hi"), ("assistant", "Hey Jai!\n\nWhat's on your plate today?"),
    ]
    prompt = fake_llm.calls[-1]
    assert isinstance(prompt[0], SystemMessage) and "You are Mavis" in prompt[0].content
    assert isinstance(prompt[-1], HumanMessage) and prompt[-1].content == "hi"


async def test_history_is_included(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Noted.")
    await run_turn(msg_event(user.id, "my friend Jawahar is helping me", "e1"))
    fake_llm.push_text("Jawahar!")
    await run_turn(msg_event(user.id, "who's helping me?", "e2"))
    contents = [m.content for m in fake_llm.calls[-1][1:]]
    assert contents == ["my friend Jawahar is helping me", "Noted.", "who's helping me?"]


async def test_start_command_adds_greeting_hint(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, None)
    fake_llm.push_text("Hey! I'm Mavis.")
    await run_turn(msg_event(user.id, "/start", command="start"))
    assert "just opened the chat" in fake_llm.calls[-1][0].content


async def test_file_message_is_described(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Got it.")
    await run_turn(msg_event(user.id, "", file={"file_id": "f", "file_name": "cv.pdf"}))
    assert fake_llm.calls[-1][-1].content == "[sent a file: cv.pdf]"


async def test_run_turn_is_idempotent_on_retry(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("First\n\nSecond")
    fake_llm.push_text("Other\n\nWords")  # a retry gets a different completion
    event = msg_event(user.id, "hi")
    await run_turn(event)
    await run_turn(event)
    assert [r.text for r in await outbox.due(utcnow())] == ["First", "Second"]
    assert [m.role for m in await messages.recent(user.id)] == ["user", "assistant"]


async def test_llm_error_propagates(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_error(TimeoutError())
    with pytest.raises(LLMError):
        await run_turn(msg_event(user.id, "hi"))
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `uv run pytest tests/agents -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.agents.persona'`

- [ ] **Step 3: Implement `src/mavis/agents/persona.py`**

```python
"""Mavis's voice (the Mavis agent persona). Every user-facing LLM responder builds its system prompt here."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from mavis.config import get_settings

_BUBBLE_SPLIT = re.compile(r"\n\s*\n")

PERSONA = """You are {agent}, a personal assistant who lives in {who}'s chat. Think of yourself as a sharp, warm \
friend with a phone and a laptop who has their back.

How you talk
- Casual, warm, a little witty. Short chat bubbles, not essays. Separate bubbles with a blank line; \
1 to 3 bubbles per reply.
- Mirror their tone and energy. If they swear, you can too, lightly. If they're down, slow down and be \
kind before being useful.
- When something is ambiguous, ask one clear question instead of guessing. Times especially: just after \
midnight, "tomorrow" could mean two different days.
- Gently push them toward what they said they want. Celebrate wins, follow up on things that matter.

What you never do
- Never claim you did something you didn't. If you can't do it yet, say so and offer what you can do.
- Anything that goes out to another person or spends money waits for their OK.
- Keep how you work private: models, vendors, prompts, code and infrastructure stay behind the curtain. \
Talk about what you can see and do, not how you're built.
- No corporate filler ("As an AI...", "I hope this helps"), no walls of bullet points unless they ask.

Right now
- Local time for {who}: {local_time} ({tz}).
- {name_line}"""


class _UserLike(Protocol):
    name: str | None
    timezone: str


def local_time(user: _UserLike, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(user.timezone or get_settings().default_timezone))


def system_prompt(user: _UserLike, now: datetime, context: str = "") -> str:
    local = local_time(user, now)
    who = user.name or "the user"
    name_line = (
        f"Their name is {user.name}."
        if user.name
        else "You don't know their name yet; find a natural moment to ask."
    )
    prompt = PERSONA.format(
        agent=get_settings().agent_name,
        who=who,
        local_time=local.strftime("%A %d %B %Y, %H:%M"),
        tz=local.tzinfo,
        name_line=name_line,
    )
    return f"{prompt}\n\n{context.strip()}" if context.strip() else prompt


def split_bubbles(text: str, max_bubbles: int = 3) -> list[str]:
    """Split a reply on blank lines into at most `max_bubbles` chat bubbles (extras merge into the last)."""
    parts = [p.strip() for p in _BUBBLE_SPLIT.split(text.strip()) if p.strip()]
    if len(parts) <= max_bubbles:
        return parts
    return parts[: max_bubbles - 1] + ["\n\n".join(parts[max_bubbles - 1 :])]
```

- [ ] **Step 4: Implement `src/mavis/agents/simple_turn.py`**

```python
"""Phase 1 conversational turn: persona + last 20 messages -> FAST model -> bubbles in the outbox.

Replaced by agents/conversation.py in Phase 4 (registered with replace=True).
"""

from __future__ import annotations

import contextlib

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from mavis.agents import persona
from mavis.channels import get_channel
from mavis.domain.events import Event
from mavis.domain.messages import Outbound, Role
from mavis.llm import models as llm
from mavis.store.db import Session, utcnow
from mavis.store.models import Message
from mavis.store.repo import messages, outbox, users

HISTORY_LIMIT = 20
START_HINT = (
    "The user just opened the chat with /start. Greet them warmly, introduce yourself in one line, "
    "and ask what's on their plate right now."
)


def user_text(event: Event) -> str:
    text = (event.payload.get("text") or "").strip()
    file = event.payload.get("file")
    if file:
        note = f"[sent a file: {file.get('file_name', 'file')}]"
        text = f"{text}\n{note}".strip() if text else note
    return text


def _to_langchain(history: list[Message]) -> list[BaseMessage]:
    return [HumanMessage(m.content) if m.role == Role.USER.value else AIMessage(m.content) for m in history]


async def run_turn(event: Event) -> None:
    user = await users.get(event.user_id)
    text = user_text(event)
    await messages.log(user.id, Role.USER, text, event_id=event.id)

    if user.telegram_chat_id is not None:
        with contextlib.suppress(Exception):
            await get_channel().send_typing(user.telegram_chat_id)

    history = await messages.recent(user.id, HISTORY_LIMIT)
    hint = START_HINT if event.payload.get("command") == "start" else ""
    prompt: list[BaseMessage] = [SystemMessage(persona.system_prompt(user, utcnow(), context=hint))]
    prompt += _to_langchain(history)

    reply = await llm.complete(prompt, llm.Tier.FAST, name="simple_turn")
    bubbles = persona.split_bubbles(reply) or [reply]

    async with Session() as s:
        for i, bubble in enumerate(bubbles):
            await outbox.enqueue(s, Outbound(user_id=user.id, text=bubble, dedupe_key=f"reply:{event.id}:{i}"))
        await s.commit()
    await messages.log(user.id, Role.ASSISTANT, "\n\n".join(bubbles), event_id=f"reply:{event.id}")
```

- [ ] **Step 5: Implement `src/mavis/worker/handlers.py`**

```python
"""Default handler wiring for this phase. Later phases add their registrations here."""

from __future__ import annotations

from mavis.agents import simple_turn
from mavis.domain.events import EventType
from mavis.worker.runner import register_event_handler


def register_default_handlers() -> None:
    register_event_handler(EventType.USER_MESSAGE, simple_turn.run_turn)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/agents -v`
Expected: `9 passed`

- [ ] **Step 7: Commit**

```bash
git add src/mavis/agents src/mavis/worker/handlers.py tests/agents
git commit -m "feat(agents): Mavis persona and simple chat turn with working memory via the outbox"
```

---

### Task 12: Telegram ingestion, the FastAPI app, health routes, and the dev poller

**Files:**
- Create: `src/mavis/channels/telegram_updates.py`, `src/mavis/channels/telegram_poller.py`
- Create: `src/mavis/api/app.py`, `src/mavis/api/routes/telegram.py`, `src/mavis/api/routes/health.py`
- Create: `tests/api/__init__.py`, `tests/api/test_telegram_webhook.py`, `tests/api/test_health.py`, `tests/channels/test_telegram_poller.py`

**Interfaces:**
- Consumes: `EventBus`, `get_bus()`, `get_redis()`, `users.get_or_create_by_chat`, `db.ping`, `init_db`, `dispose_engine`, `configure_logging`
- Produces:
  - `mavis.channels.telegram_updates.ingest_update(data: dict, bus: EventBus) -> bool`. Payloads:
    - `USER_MESSAGE`: `{"text", "message_id", "command"?, "file"?: InboundFile dict}`
    - `BUTTON_PRESSED`: `{"data", "callback_query_id", "message_id"}`
    - id `f"tg:update:{update_id}"`, `trust=USER`
  - `mavis.channels.telegram_poller.run_polling(bus, token, bot=None)`
  - `mavis.api.app.create_app() -> FastAPI`
  - `mavis.api.routes.health.register_readiness_check(name, fn)`; `GET /health/live`, `GET /health/ready`
  - `POST /webhooks/telegram`

- [ ] **Step 1: Write the failing tests**

`tests/api/__init__.py`: empty file.

`tests/api/test_telegram_webhook.py`:

```python
import httpx
import pytest

from mavis.api.app import create_app
from mavis.config import get_settings
from mavis.domain.events import EventType, Trust
from mavis.store.repo import users


def update(update_id: int, chat_id: int = 100, text: str = "hi", **extra) -> dict:
    msg = {"message_id": 5, "date": 1790930000, "chat": {"id": chat_id, "type": "private"},
           "from": {"id": chat_id, "first_name": "Jai"}, "text": text}
    msg.update(extra)
    return {"update_id": update_id, "message": msg}


@pytest.fixture
async def client(db, bus):
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _published(bus) -> list:
    items = []
    while not bus._events.empty():
        event, _ = bus._events.get_nowait()
        bus._events.task_done()
        items.append(event)
    return items


async def test_message_update_published_as_user_message(client, bus) -> None:
    r = await client.post("/webhooks/telegram", json=update(1, text="hello"))
    assert r.status_code == 200 and r.json() == {"ok": True, "published": True}
    [event] = await _published(bus)
    user = await users.get_by_chat(100)
    assert event.id == "tg:update:1" and event.type is EventType.USER_MESSAGE
    assert event.user_id == user.id and user.name == "Jai"
    assert event.payload["text"] == "hello" and event.trust is Trust.USER
    assert event.occurred_at.tzinfo is not None


async def test_duplicate_update_published_once(client, bus) -> None:
    await client.post("/webhooks/telegram", json=update(2))
    r = await client.post("/webhooks/telegram", json=update(2))
    assert r.json()["published"] is False
    assert len(await _published(bus)) == 1


async def test_start_command_and_document(client, bus) -> None:
    await client.post("/webhooks/telegram", json=update(3, text="/start@Mavis247_bot"))
    await client.post("/webhooks/telegram", json=update(
        4, text=None, caption="read this",
        document={"file_id": "F1", "file_name": "cv.pdf", "mime_type": "application/pdf", "file_size": 12},
    ))
    start, doc = await _published(bus)
    assert start.payload["command"] == "start"
    assert doc.payload["text"] == "read this"
    assert doc.payload["file"] == {"file_id": "F1", "file_name": "cv.pdf", "mime_type": "application/pdf",
                                   "size": 12}


async def test_photo_uses_largest_size(client, bus) -> None:
    await client.post("/webhooks/telegram", json=update(
        5, text=None, photo=[{"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
                             {"file_id": "big", "file_unique_id": "b", "width": 1280, "height": 1280}],
    ))
    [event] = await _published(bus)
    assert event.payload["file"]["file_id"] == "big"


async def test_callback_query_published_as_button_pressed(client, bus) -> None:
    body = {"update_id": 6, "callback_query": {
        "id": "cq1", "from": {"id": 100, "first_name": "Jai"}, "data": "appr:1:yes",
        "message": {"message_id": 9, "date": 1790930000, "chat": {"id": 100, "type": "private"}},
    }}
    await client.post("/webhooks/telegram", json=body)
    [event] = await _published(bus)
    assert event.type is EventType.BUTTON_PRESSED
    assert event.payload == {"data": "appr:1:yes", "callback_query_id": "cq1", "message_id": 9}


async def test_secret_token_enforced(client, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    get_settings.cache_clear()
    assert (await client.post("/webhooks/telegram", json=update(7))).status_code == 403
    r = await client.post("/webhooks/telegram", json=update(7),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
    assert r.status_code == 200


async def test_disallowed_chat_ignored(client, bus, monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_TELEGRAM_CHAT_IDS", "[999]")
    get_settings.cache_clear()
    r = await client.post("/webhooks/telegram", json=update(8, chat_id=100))
    assert r.json()["published"] is False
    assert await _published(bus) == []
    assert await users.get_by_chat(100) is None


async def test_unknown_update_type_ignored(client, bus) -> None:
    r = await client.post("/webhooks/telegram", json={"update_id": 9, "poll": {}})
    assert r.json()["published"] is False
```

`tests/api/test_health.py`:

```python
import httpx

from mavis.api.app import create_app
from mavis.api.routes.health import register_readiness_check


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


async def test_live_and_ready(db, bus) -> None:
    async with await _client() as c:
        assert (await c.get("/health/live")).json() == {"status": "ok"}
        r = await c.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": True, "bus": True}}


async def test_ready_degrades_when_a_check_fails(db, bus) -> None:
    async def broken() -> bool:
        raise RuntimeError("neo4j down")

    register_readiness_check("neo4j", broken)
    try:
        async with await _client() as c:
            r = await c.get("/health/ready")
        assert r.status_code == 503 and r.json()["checks"]["neo4j"] is False
    finally:
        from mavis.api.routes import health

        health._checks.pop("neo4j", None)
```

`tests/channels/test_telegram_poller.py`:

```python
import asyncio
from types import SimpleNamespace

from mavis.channels.telegram_poller import run_polling


class PollingBot:
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = batches
        self.offsets: list = []
        self.webhook_deleted = False

    async def initialize(self) -> None:
        pass

    async def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self.webhook_deleted = True

    async def get_updates(self, offset=None, timeout=0, allowed_updates=None):
        self.offsets.append(offset)
        if not self.batches:
            await asyncio.sleep(3600)
        return [SimpleNamespace(update_id=d["update_id"], to_dict=lambda d=d: d) for d in self.batches.pop(0)]


async def test_poller_feeds_ingest_and_advances_offset(db, bus) -> None:
    upd = {"update_id": 41, "message": {"message_id": 1, "date": 1790930000,
                                        "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "yo"}}
    bot = PollingBot([[upd]])
    task = asyncio.create_task(run_polling(bus, "token", bot=bot))
    for _ in range(100):
        if len(bot.offsets) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert bot.webhook_deleted
    assert bot.offsets[:2] == [None, 42]
    event, _ = bus._events.get_nowait()
    assert event.id == "tg:update:41"
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `uv run pytest tests/api tests/channels/test_telegram_poller.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.api.app'`

- [ ] **Step 3: Implement `src/mavis/channels/telegram_updates.py`**

```python
"""Raw Telegram Update dict -> Event. Shared by the webhook route and the dev poller."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import structlog

from mavis.bus.base import EventBus
from mavis.config import get_settings
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.messages import InboundFile
from mavis.store.db import utcnow
from mavis.store.repo import users

log = structlog.get_logger(__name__)


def _file(msg: dict[str, Any]) -> InboundFile | None:
    if doc := msg.get("document"):
        return InboundFile(file_id=doc["file_id"], file_name=doc.get("file_name") or "document",
                           mime_type=doc.get("mime_type"), size=doc.get("file_size"))
    if photos := msg.get("photo"):
        best = max(photos, key=lambda p: p.get("width", 0) * p.get("height", 0))
        return InboundFile(file_id=best["file_id"], file_name=f"photo_{best.get('file_unique_id', 'x')}.jpg",
                           mime_type="image/jpeg", size=best.get("file_size"))
    return None


async def ingest_update(data: dict[str, Any], bus: EventBus) -> bool:
    """Normalise and publish one update. Returns True if a new event was published."""
    update_id = data.get("update_id")
    if update_id is None:
        return False

    if cq := data.get("callback_query"):
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        sender = cq.get("from") or {}
        event_type = EventType.BUTTON_PRESSED
        payload: dict[str, Any] = {"data": cq.get("data", ""), "callback_query_id": cq.get("id"),
                                   "message_id": message.get("message_id")}
        occurred = utcnow()
    elif msg := data.get("message") or data.get("edited_message"):
        chat_id = (msg.get("chat") or {}).get("id")
        sender = msg.get("from") or {}
        event_type = EventType.USER_MESSAGE
        text = msg.get("text") or msg.get("caption") or ""
        payload = {"text": text, "message_id": msg.get("message_id")}
        if text.startswith("/"):
            payload["command"] = text[1:].split()[0].split("@")[0].lower()
        if file := _file(msg):
            payload["file"] = file.model_dump()
        occurred = datetime.fromtimestamp(msg.get("date") or time.time(), UTC)
    else:
        return False

    if chat_id is None:
        return False
    allowed = get_settings().allowed_telegram_chat_ids
    if allowed and chat_id not in allowed:
        log.warning("telegram.chat_not_allowed", chat_id=chat_id)
        return False

    user, _ = await users.get_or_create_by_chat(chat_id, sender.get("first_name"))
    event = Event(id=f"tg:update:{update_id}", user_id=user.id, type=event_type, occurred_at=occurred,
                  source="telegram", payload=payload, trust=Trust.USER)
    return await bus.publish(event)
```

- [ ] **Step 4: Implement `src/mavis/channels/telegram_poller.py`**

```python
"""Long-polling for dev (no public URL needed). Feeds the same normaliser as the webhook."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from telegram import Bot

from mavis.bus.base import EventBus
from mavis.channels.telegram_updates import ingest_update

log = structlog.get_logger(__name__)
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]


async def run_polling(bus: EventBus, token: str, bot: Any | None = None) -> None:
    bot = bot or Bot(token)
    await bot.initialize()
    await bot.delete_webhook(drop_pending_updates=False)
    offset: int | None = None
    backoff = 1.0
    log.info("telegram.polling_started")
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=25, allowed_updates=ALLOWED_UPDATES)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram.get_updates_failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        for upd in updates:
            try:
                await ingest_update(upd.to_dict(), bus)
            except Exception:
                log.exception("telegram.ingest_failed", update_id=upd.update_id)
            offset = upd.update_id + 1
```

- [ ] **Step 5: Implement `src/mavis/api/routes/health.py`**

```python
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from mavis.bus import get_redis
from mavis.store import db

router = APIRouter()
ReadinessCheck = Callable[[], Awaitable[bool]]
_checks: dict[str, ReadinessCheck] = {}


def register_readiness_check(name: str, fn: ReadinessCheck) -> None:
    """Later phases add Neo4j / Qdrant / provider checks here."""
    _checks[name] = fn


async def _bus_ping() -> bool:
    client = get_redis()
    return True if client is None else bool(await client.ping())


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready() -> JSONResponse:
    results: dict[str, bool] = {}
    for name, fn in {"database": db.ping, "bus": _bus_ping, **_checks}.items():
        try:
            results[name] = bool(await asyncio.wait_for(fn(), timeout=3))
        except Exception:  # noqa: BLE001 - any failure means not ready
            results[name] = False
    ok = all(results.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": results}, status_code=200 if ok else 503)
```

- [ ] **Step 6: Implement `src/mavis/api/routes/telegram.py`**

```python
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, Request

from mavis.bus import get_bus
from mavis.channels.telegram_updates import ingest_update
from mavis.config import get_settings

router = APIRouter()


@router.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    secret = get_settings().telegram_webhook_secret
    if secret and not hmac.compare_digest(x_telegram_bot_api_secret_token or "", secret):
        raise HTTPException(status_code=403, detail="bad secret token")
    published = await ingest_update(await request.json(), get_bus())
    return {"ok": True, "published": published}
```

- [ ] **Step 7: Implement `src/mavis/api/app.py`**

```python
"""FastAPI app for the `api` role: webhooks + health. Never calls an LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mavis.api.routes import health, telegram
from mavis.bus import get_bus
from mavis.config import get_settings
from mavis.logging import configure_logging
from mavis.store.db import dispose_engine, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    s = get_settings()
    if s.telegram_mode == "webhook" and not s.telegram_webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")
    if s.is_sqlite:
        await init_db()  # Postgres schemas are managed by `mavis migrate`
    app.state.bus = get_bus()
    yield
    await get_bus().close()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Mavis", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(telegram.router)
    return app
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/api tests/channels/test_telegram_poller.py -v`
Expected: `11 passed`

- [ ] **Step 9: Commit**

```bash
git add src/mavis/channels/telegram_updates.py src/mavis/channels/telegram_poller.py src/mavis/api tests/api tests/channels/test_telegram_poller.py
git commit -m "feat(api): telegram webhook + poller normalising updates to events, health endpoints"
```

---

### Task 13: CLI (`dev`, `api`, `worker`, `chat`, `migrate`)

**Files:**
- Create: `src/mavis/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above
- Produces:
  - Typer `app` with commands `dev`, `api`, `worker`, `chat`, `migrate`
  - `mavis.cli.bootstrap(create_tables: bool) -> EventBus` (Phase 3 extends `dev`/`worker` with the timer)
  - `LOCAL_CHAT_ID = -1`

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
from typer.testing import CliRunner

from mavis.cli import app


def test_cli_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("dev", "api", "worker", "chat", "migrate"):
        assert command in result.output


def test_migrate_runs_against_temp_db(settings) -> None:
    result = CliRunner().invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert (settings.data_dir.parent / "test.db").exists()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mavis.cli'`

- [ ] **Step 3: Implement `src/mavis/cli.py`**

```python
"""Process entrypoints. One image, many roles: `mavis api | worker | dev | chat | migrate`."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from uuid import uuid4

import structlog
import typer

from mavis.bus import get_bus, set_bus
from mavis.bus.base import EventBus
from mavis.config import get_settings
from mavis.logging import configure_logging
from mavis.store.db import dispose_engine, init_db

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Mavis personal assistant")
log = structlog.get_logger(__name__)
LOCAL_CHAT_ID = -1


async def bootstrap(create_tables: bool) -> EventBus:
    """Common start-up for worker-like roles: logging, schema (dev), handlers, bus."""
    from mavis.worker.handlers import register_default_handlers

    configure_logging()
    if create_tables:
        await init_db()
    register_default_handlers()
    return get_bus()


async def _run_tasks(tasks: list[asyncio.Task], bus: EventBus) -> None:
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        await bus.close()
        await dispose_engine()


async def _dev() -> None:
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.channels.telegram_poller import run_polling
    from mavis.worker.runner import run_worker

    bus = await bootstrap(create_tables=True)
    s = get_settings()
    tasks = [
        asyncio.create_task(run_worker(bus, "dev")),
        asyncio.create_task(OutboxSender().run_forever()),
    ]
    if s.telegram_bot_token:
        tasks.append(asyncio.create_task(run_polling(bus, s.telegram_bot_token)))
    else:
        log.warning("dev.no_telegram_token", hint="set TELEGRAM_BOT_TOKEN or use `mavis chat`")
    log.info("dev.started", telegram=bool(s.telegram_bot_token))
    await _run_tasks(tasks, bus)


async def _worker(name: str) -> None:
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.worker.runner import run_worker

    bus = await bootstrap(create_tables=get_settings().is_sqlite)
    tasks = [asyncio.create_task(run_worker(bus, name)), asyncio.create_task(OutboxSender().run_forever())]
    log.info("worker.started", consumer=name)
    await _run_tasks(tasks, bus)


async def _chat() -> None:
    from mavis.bus.inprocess import InProcessBus
    from mavis.channels import set_channel
    from mavis.channels.fake import ConsoleChannel
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.domain.events import Event, EventType, Trust
    from mavis.store.db import utcnow
    from mavis.store.repo import users
    from mavis.worker.handlers import register_default_handlers
    from mavis.worker.runner import run_worker

    configure_logging(level="WARNING")
    await init_db()
    register_default_handlers()
    bus = InProcessBus()
    set_bus(bus)
    console = ConsoleChannel()
    set_channel(console)
    user, _ = await users.get_or_create_by_chat(LOCAL_CHAT_ID, None)
    worker = asyncio.create_task(run_worker(bus, "chat"))
    sender = OutboxSender(console)
    print(f"Chatting with {get_settings().agent_name} locally. /quit to exit.")
    try:
        while True:
            line = (await asyncio.to_thread(input, "\nyou> ")).strip()
            if line in {"/quit", "/exit"}:
                break
            if not line:
                continue
            payload: dict = {"text": line}
            if line.startswith("/"):
                payload["command"] = line[1:].split()[0].lower()
            await bus.publish(Event(id=f"cli:{uuid4()}", user_id=user.id, type=EventType.USER_MESSAGE,
                                    occurred_at=utcnow(), source="cli", payload=payload, trust=Trust.USER))
            await bus.wait_idle()
            await sender.run_once()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        worker.cancel()
        await bus.close()
        await dispose_engine()


@app.command()
def dev() -> None:
    """Run every role in one process (Telegram long-polling, worker, outbox sender)."""
    asyncio.run(_dev())


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the webhook/health API (stateless; scale horizontally)."""
    import uvicorn

    uvicorn.run("mavis.api.app:create_app", factory=True, host=host, port=port, proxy_headers=True)


@app.command()
def worker(name: str = typer.Option(default_factory=lambda: f"worker-{socket.gethostname()}")) -> None:
    """Consume events and jobs and deliver the outbox."""
    asyncio.run(_worker(name))


@app.command()
def chat() -> None:
    """Local REPL with the agent (Mavis) — no Telegram needed."""
    asyncio.run(_chat())


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply database migrations (required for Postgres)."""
    from mavis.store.migrate import upgrade

    upgrade(get_settings().db_url, revision)
    typer.echo(f"migrated to {revision}")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli.py -v`
Expected: `2 passed`

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all tests pass (≈ 106); `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/mavis/cli.py tests/test_cli.py
git commit -m "feat(cli): dev, api, worker, chat and migrate entrypoints"
```

---

### Task 14: Manual verification with a real model and a real bot

No code; this proves the phase's demo works.

**Files:**
- Modify (local only, never committed): `.env`

**Interfaces:**
- Consumes: the whole phase
- Produces: a verified Phase 1 demo

- [ ] **Step 1: Local REPL against the real model**

Make sure `.env` has `OLLAMA_API_KEY`. Run:

```bash
uv run mavis chat
```

Type `/start`, then `I'm Jai, prepping for interviews`, then `what did I just say I'm doing?`.
Expected: a warm greeting in 1–3 bubbles, then a reply that recalls the interview prep (from working memory). `/quit` exits cleanly.

- [ ] **Step 2: Create the Telegram bot**

In Telegram, message @BotFather → `/newbot` → pick a name and username → copy the token. Add to `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_MODE=polling
```

- [ ] **Step 3: Run dev mode and chat from your phone**

```bash
uv run mavis dev
```

Expected log lines: `dev.started telegram=True`, `telegram.polling_started`. In Telegram, open your bot and send `/start`.
Expected: "typing…" appears immediately, then Mavis greets you within ~2 s (bot: @Mavis247_bot).

- [ ] **Step 4: Lock the bot to your chat**

Find your chat id with `sqlite3 data/mavis.db "select telegram_chat_id from users"`. Set `ALLOWED_TELEGRAM_CHAT_IDS=[<id>]` in `.env` and restart `mavis dev`. Messages from any other account are now ignored (log: `telegram.chat_not_allowed`).

- [ ] **Step 5: Restart resilience**

Send a message, stop `mavis dev` with Ctrl-C right after the "typing…" indicator appears, then start it again.
Expected: the reply still arrives exactly once, either from the outbox if it was already queued or after Telegram re-delivers the update. Then send "what were we talking about?". Mavis answers from the persisted history.

- [ ] **Step 6: Check logs for secrets**

```bash
uv run mavis dev 2>&1 | head -50 | grep -Ei "$(grep TELEGRAM_BOT_TOKEN .env | cut -d= -f2 | cut -c1-12)" || echo "no token in logs"
```

Expected: `no token in logs`.

- [ ] **Step 7: Tag the phase**

```bash
git tag phase-1-foundation
```
