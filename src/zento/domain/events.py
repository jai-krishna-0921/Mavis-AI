from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    USER_MESSAGE = "user_message"
    BUTTON_PRESSED = "button_pressed"
    EMAIL_RECEIVED = "email_received"
    CALENDAR_CHANGED = "calendar_changed"
    EVENT_STARTING = "event_starting"
    EVENT_ENDED = "event_ended"
    SLACK_MESSAGE = "slack_message"
    NOTION_CHANGED = "notion_changed"
    CONNECTION_CHANGED = "connection_changed"
    WAKEUP = "wakeup"
    USER_QUIET = "user_quiet"
    TASK_COMPLETED = "task_completed"
    TASK_PROGRESS = "task_progress"
    LOOP_CREATED = "loop_created"
    LOOP_UPDATED = "loop_updated"


class Trust(StrEnum):
    USER = "user"          # typed by the user
    SYSTEM = "system"      # produced by Zento itself
    UNTRUSTED = "untrusted"  # third-party content (email, web, slack, files)


class Event(BaseModel):
    id: str = Field(description="Idempotency key, e.g. 'tg:update:123'")
    user_id: int
    type: EventType
    occurred_at: datetime
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    trust: Trust = Trust.SYSTEM


class JobKind(StrEnum):
    LEARN = "learn"                 # P2: memory extraction for a turn/signal
    CONSOLIDATE = "consolidate"     # P2
    RUN_TASK = "run_task"           # P4: orchestrator task
    RESUME_TASK = "resume_task"     # P4: resume interrupted run
    CONNECTION_CHECK = "connection_check"  # P5
    FIRST_SYNC = "first_sync"       # P5
    POLL_PROVIDER = "poll_provider"  # P5


class Job(BaseModel):
    id: str
    user_id: int
    kind: JobKind
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
