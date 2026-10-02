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
    # tried in order when the tier's primary model times out / is unreachable / 5xx / 429
    model_fast_fallbacks: list[str] = ["gemma4:31b", "gpt-oss:120b"]
    model_smart_fallbacks: list[str] = ["gpt-oss:20b"]
    # process-wide cap on in-flight LLM calls (Ollama Cloud free tier 429s on concurrent requests)
    llm_max_concurrency: int = 1
    llm_timeout_fast_s: float = 12.0
    llm_timeout_smart_s: float = 60.0

    # --- Telegram -------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_mode: Literal["polling", "webhook"] = "polling"
    allowed_telegram_chat_ids: list[int] = Field(default_factory=list)

    # --- bus / worker ---------------------------------------------------------
    bus_claim_idle_ms: int = 900_000  # redeliver an unacked message after this idle time (> longest handler)
    worker_concurrency: int = 4  # consumer loops per stream per worker process

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
    composio_base_url: str = "https://backend.composio.dev/api/v3"
    composio_timeout_s: float = 30.0
    integration_polling: bool = False
    integration_status_ttl_s: int = 60
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
    onboarding_quiet_hours: float = 4.0  # nudge if the user hasn't answered a question after this long
    morning_checkin_time: str = "08:30"  # default local time for the morning check-in routine
    timer_interval_s: float = 5.0  # how often the timer role claims due wakeups

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
