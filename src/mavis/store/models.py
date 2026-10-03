"""ORM tables. Later phases append their tables here AND add a matching Alembic revision."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
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


class Task(Base):
    __tablename__ = "tasks"

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
    plan: Mapped[dict | None] = mapped_column(JSON)
    result_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


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
    resolved_at: Mapped[datetime | None]


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
