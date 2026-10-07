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
    model_fast: str = "deepseek-v4.1-flash"
    model_smart: str = "glm-5.3"
    # tried in order ONLY on model-specific errors (404 / 5xx); timeouts and 429 are account-wide
    model_fast_fallbacks: list[str] = ["glm-5.3-flash", "gpt-oss:120b"]
    model_smart_fallbacks: list[str] = ["kimi-k3", "deepseek-v4.1-flash"]
    # process-wide cap on in-flight LLM calls (Ollama Cloud free tier 429s on concurrent requests)
    llm_max_concurrency: int = 3
    llm_timeout_fast_s: float = 30.0
    llm_timeout_smart_s: float = 60.0
    # after a client-side timeout the request still runs server-side and holds the account's slot
    llm_timeout_cooldown_s: float = 45.0
    # sent as reasoning_effort to FAST-tier gpt-oss models only, when non-empty (low/medium/high)
    llm_reasoning_effort_fast: str = "low"
    # optional secondary OpenAI-compatible provider (e.g. Groq); off while base url / model are empty
    llm_secondary_base_url: str = ""
    llm_secondary_api_key: str = ""
    llm_secondary_model_fast: str = ""
    llm_secondary_model_smart: str = ""
    llm_secondary_max_concurrency: int = 2
    # emoji reacted onto each incoming Telegram message; empty = off (typing indicator stays)
    presence_reaction: str = ""

    # --- Telegram -------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_mode: Literal["polling", "webhook"] = "polling"
    allowed_telegram_chat_ids: list[int] = Field(default_factory=list)
    # The live E2E harness's own chat: admitted beside the allowlist, and every send to it goes to a log
    # sink (data_dir/test_sink.jsonl), never to Telegram, so tests never write into a real user's chat.
    # Off unless enabled, and the id must be synthetic (below -10**15) and not allowlisted
    # (channels.test_sink.active_test_chat).
    live_test_enabled: bool = False
    test_telegram_chat_id: int | None = None

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
    web_search_enabled: bool = True  # WEB capability: Tavily when keyed, else DuckDuckGo; False turns it off
    # Google Workspace through Composio's googlesuper toolkit (spec 2026-10-03). Off: behaviour is exactly
    # the Gmail + Calendar setup from before. Prod turns it on in compose after the verify script passes.
    google_workspace_enabled: bool = False
    workspace_poll_minutes: int = 30  # Tasks due/overdue and Drive shared-with-me safety-net polls

    # --- orchestration (Phase 4) ----------------------------------------------
    task_timeout_s: float = 480
    task_max_concurrency: int = 1
    task_progress_after_s: float = 30
    approval_ttl_hours: int = 48
    react_max_steps: int = 8
    # Tool rounds for the research specialist. 6 cut task #14 off after 33 s with its searches thrown
    # away; the task models (glm-5.3 and fallbacks) tend to issue one search or extract per round, and a
    # run that hits the limit now wraps up with what it gathered instead of failing.
    research_max_steps: int = 10
    # A failed approval blocks the loops created in its own chat turn and in the user's turn just before
    # it, but only when that previous turn came within this many minutes (older turns are unrelated).
    failed_turn_link_minutes: int = 30
    tool_timeout_s: float = 45  # per tool call inside react_loop; a tool may override via metadata
    task_step_parallelism: int = 1
    spawn_max_per_step: int = 3  # spawned workers one model step may start
    initiative_act_enabled: bool = True

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
    quiet_awake_window_min: int = 60  # recent user message lifts quiet hours
    demo_time_scale: float = 1.0
    onboarding_quiet_hours: float = 4.0  # nudge if the user hasn't answered a question after this long
    morning_checkin_time: str = "08:30"  # default local time for the morning check-in routine
    timer_interval_s: float = 5.0  # how often the timer role claims due wakeups

    # --- attention layer (spec 2026-10-03) ------------------------------------
    attention_enabled: bool = True  # false: the Phase 5 email path is used unchanged
    attention_understand_per_window: int = 4  # LLM understanding attempts per user per window
    attention_window_s: int = 120  # matches the 2 min Gmail poll interval
    attention_max_attempts: int = 2  # LLM attempts per email before the heuristic fallback
    attention_currency: str = "INR"  # the user's currency, used when an email states none
    # cold start: with no history, a debit at or above this amount (per currency) is notable
    attention_large_amounts: dict[str, float] = {"INR": 10000.0, "USD": 150.0, "EUR": 150.0, "GBP": 120.0}
    attention_ask_threshold: float = 0.6
    attention_notify_threshold: float = 0.7
    attention_brief_threshold: float = 0.35
    attention_pref_similarity: float = 0.8
    attention_allow_urgent: bool = True  # deterministic high-risk asks may use urgency 5 (spec 8.4)
    attention_retention_days: int = 90
    attention_strict_errors: bool = False  # tests only: per-row catch-alls re-raise instead of logging
    attention_backfill_max: int = 120  # messages, paged 50 at a time, within 14 days
    attention_digest_hours: int = 24
    attention_evening_enabled: bool = True
    attention_evening_time: str = "20:30"

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
