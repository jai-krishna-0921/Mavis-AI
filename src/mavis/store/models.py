"""ORM tables. Later phases append their tables here AND add a matching Alembic revision."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    false as sa_false,
)
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
    photo_path: Mapped[str | None] = mapped_column(String(1024))
    media: Mapped[list[Any] | None] = mapped_column(JSON, default=list)
    proactive: Mapped[bool] = mapped_column(default=False)
    # chat handle of a non-default channel (Slack)
    route: Mapped[str | None] = mapped_column(String(200), default=None)
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


class GraphNode(Base):
    """SQLite/Postgres fallback graph: one row per entity in the user's world."""

    __tablename__ = "graph_nodes"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_graph_nodes_user_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    key: Mapped[str] = mapped_column(String(240), index=True)
    name: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(40))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)


class GraphEdge(Base):
    """Bi-temporal-lite edge: valid_to NULL means the fact is current."""

    __tablename__ = "graph_edges"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    src_key: Mapped[str] = mapped_column(String(240), index=True)
    rel: Mapped[str] = mapped_column(String(40))
    dst_key: Mapped[str] = mapped_column(String(240), index=True)
    statement: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    valid_from: Mapped[datetime] = mapped_column(default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(default=None, index=True)
    source_ref: Mapped[str] = mapped_column(String(200), default="")


class ProfileCardRow(Base):
    __tablename__ = "profile_cards"
    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_profile_cards_user_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    upto_message_id: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LoopRow(Base):
    """An open loop: something unfinished the assistant keeps track of."""

    __tablename__ = "loops"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(300))
    due_at: Mapped[datetime | None] = mapped_column(default=None, index=True)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="OPEN", index=True)
    importance: Mapped[int] = mapped_column(Integer, default=3)
    watch: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Provenance (phase A): explicit, never inferred from `source`. Rows from before it read as untrusted.
    trust: Mapped[str] = mapped_column(String(12), default="untrusted", server_default="untrusted")
    origin: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    # The source the loop was CREATED from (a chat turn's event id), never overwritten by a later merge
    # (`source` is). A failed action blocks only loops created in its turns (hotfix4 H1).
    created_ref: Mapped[str | None] = mapped_column(String(200))
    # While BLOCKED: the failed approval that blocked it ("approval:9"), so a later success reopens it.
    blocked_by: Mapped[str | None] = mapped_column(String(40))


class WakeupRow(Base):
    """An alarm the agent set for itself."""

    __tablename__ = "wakeups"
    __table_args__ = (
        Index("ix_wakeups_status_due", "status", "due_at"),
        Index(
            "uq_wakeups_pending_dedupe",
            "user_id",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status = 'pending' AND dedupe_key IS NOT NULL"),
            postgresql_where=text("status = 'pending' AND dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    due_at: Mapped[datetime] = mapped_column()
    kind: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    loop_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    dedupe_key: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column()
    fired_at: Mapped[datetime | None] = mapped_column(nullable=True)


class PingLogRow(Base):
    """Unsolicited messages actually sent, keyed per local day for dedupe."""

    __tablename__ = "ping_log"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_ping_log_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(240))
    urgency: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column()


class ConnectionPending(Base):
    """A connect link we sent and are waiting on. task_id is the interrupted run to resume, if any."""

    __tablename__ = "connections_pending"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    capability: Mapped[str] = mapped_column(String(40), index=True)
    task_id: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AttentionObservation(Base):
    """One processed email (spec attention section 11). No bodies at rest: `pending_payload` holds the
    normalized snippet only until the email is understood, then it is cleared."""

    __tablename__ = "attention_observations"
    __table_args__ = (
        UniqueConstraint("user_id", "message_id", name="uq_attention_observations_user_msg"),
        Index("ix_attention_observations_user_source", "user_id", "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(200))
    # "mail" for email; "drive", "docs", "tasks", "calendar" for Google Workspace signals (spec 2026-10-03)
    source: Mapped[str] = mapped_column(String(12), default="mail", server_default="mail")
    thread_id: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(12), default="live")
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    method: Mapped[str] = mapped_column(String(12), default="")
    sender_domain: Mapped[str] = mapped_column(String(120), default="")
    sender_name: Mapped[str] = mapped_column(String(80), default="")
    kind: Mapped[str] = mapped_column(String(24), default="other")
    needs_user: Mapped[bool] = mapped_column(default=False)
    verdict: Mapped[str] = mapped_column(String(12), default="pending")
    urgency: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(String(240), default="")
    action: Mapped[str] = mapped_column(String(160), default="")
    point_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    delivery: Mapped[str] = mapped_column(String(12), default="none")
    feedback: Mapped[str | None] = mapped_column(String(12), nullable=True)
    received_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AttentionSender(Base):
    __tablename__ = "attention_senders"
    __table_args__ = (UniqueConstraint("user_id", "address", name="uq_attention_senders_user_address"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    address: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(120), default="")
    count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column()
    last_seen: Mapped[datetime] = mapped_column()


class AttentionMoneyBaseline(Base):
    """Rolling robust stats of debits per counterparty, per method and overall ('*')."""

    __tablename__ = "attention_money_baselines"
    __table_args__ = (
        UniqueConstraint("user_id", "scope", "key", "currency", name="uq_attention_money_user_scope_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3))
    count: Mapped[int] = mapped_column(Integer, default=0)
    amounts: Mapped[list] = mapped_column(JSON, default=list)
    median: Mapped[float] = mapped_column(Float, default=0.0)
    mad: Mapped[float] = mapped_column(Float, default=0.0)
    hours: Mapped[list] = mapped_column(JSON, default=list)
    first_seen: Mapped[datetime] = mapped_column()
    last_seen: Mapped[datetime] = mapped_column()


class AttentionPref(Base):
    """User feedback on an observation, mirrored as a vector in the Qdrant 'attention' collection."""

    __tablename__ = "attention_prefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    observation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(24))
    sentiment: Mapped[str] = mapped_column(String(12))
    summary: Mapped[str] = mapped_column(String(240), default="")
    point_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    # A chat turn's start_task records its triggering event here, so a redelivered turn finds its task.
    __table_args__ = (UniqueConstraint("user_id", "source_ref", name="uq_tasks_user_source_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str] = mapped_column(String(16), default="task")
    origin: Mapped[str] = mapped_column(String(16), default="user")
    goal: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    notify_on_complete: Mapped[bool] = mapped_column(default=True)
    # True when created from a turn that saw untrusted tool output; every step loop then runs tainted.
    tainted: Mapped[bool] = mapped_column(default=False)
    source_ref: Mapped[str | None] = mapped_column(String(160))
    plan: Mapped[dict | None] = mapped_column(JSON)
    result_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # The chat turn (its event id) that produced this task; APPROVAL tasks link their approvals to it.
    turn_ref: Mapped[str | None] = mapped_column(String(160))
    # A FAILED/PARTIAL outcome stays in "recently failed" until the user acknowledges it (hotfix4 H1).
    acknowledged_at: Mapped[datetime | None]


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(default=None)  # sent to the user (once)


class TaskCard(Base):
    """The one live status message of a user task (Phase 12). Survives a resume on another worker."""

    __tablename__ = "task_cards"
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_edit_at: Mapped[datetime | None] = mapped_column(default=None)
    final: Mapped[bool] = mapped_column(default=False, server_default=sa_false())
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PendingApproval(Base):
    __tablename__ = "pending_approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict] = mapped_column(JSON)
    preview: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    prompted_at: Mapped[datetime | None]
    # Execution marker: set right before an approved tool runs. EXECUTED with started_at but no
    # resolved_at means "may have run" (crash mid-execution); EXECUTED without started_at means
    # "claimed, never run".
    started_at: Mapped[datetime | None]
    # Set when a decision moves the row to RESOLVING; the sweep uses it to spot a lost resume.
    resolving_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    # Queued by a run that saw third-party content: its arguments may be attacker-shaped, so it is
    # never merged with an untainted request for the same action (approval dedupe), nor vice versa.
    tainted: Mapped[bool] = mapped_column(default=False, server_default=sa_false())
    # Plain words for the user about why an approved action failed (FailureKind text), never a provider
    # body; `result` keeps the model-facing detail.
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    acknowledged_at: Mapped[datetime | None]


class PolicyRule(Base):
    __tablename__ = "policy_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(64))
    contains: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(index=True)
    actor: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(120))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class InitiativeDecisionRow(Base):
    """The reasoner's decision for one event, kept so a retry re-applies it instead of asking again."""

    __tablename__ = "initiative_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(200), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    decision: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(index=True)


class NativeGrant(Base):
    """One user's OAuth grant to one native provider (google | slack). Tokens are sealed (envelope
    encryption bound to user, provider and column); `account` holds non-secret facts only."""

    __tablename__ = "native_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_native_grants_user_provider"),
        UniqueConstraint("provider", "account_key", name="uq_native_grants_provider_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    account: Mapped[dict] = mapped_column(JSON, default=dict)
    account_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class NativeOAuthState(Base):
    """A consent redirect we issued. Deleting the row is what consumes the state (single use)."""

    __tablename__ = "native_oauth_states"

    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    pending_id: Mapped[int | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)


class MachineSession(Base):
    """One sandbox or browser session of a task (Phase 12). The row is how the reaper finds orphans."""

    __tablename__ = "machine_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # code | browser
    backend: Mapped[str] = mapped_column(String(24))
    session_id: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | closed | stopped
    opened_at: Mapped[datetime] = mapped_column(default=utcnow)
    deadline_at: Mapped[datetime | None] = mapped_column(default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    wall_s: Mapped[float] = mapped_column(Float, default=0.0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class ComputeUsage(Base):
    """Metered machine time per user and local day; one row per session (idempotent)."""

    __tablename__ = "compute_usage"
    __table_args__ = (UniqueConstraint("session_id", name="uq_compute_usage_session"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(16))
    task_id: Mapped[int | None] = mapped_column(Integer)
    session_id: Mapped[str] = mapped_column(String(200))
    wall_s: Mapped[float] = mapped_column(Float, default=0.0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class WorkspaceFileRow(Base):
    """Metadata of a file in a user's workspace; the bytes live in the workspace store."""

    __tablename__ = "workspace_files"
    __table_args__ = (UniqueConstraint("user_id", "path", name="uq_workspace_files_user_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    cls: Mapped[str] = mapped_column(String(8))
    provenance: Mapped[str] = mapped_column(String(24))
    task_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)


class UserQuota(Base):
    """Per-user override of a machine quota (owner or plan tooling sets it)."""

    __tablename__ = "user_quotas"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_quotas_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    value: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
