from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from mavis.domain.loops import LoopUpsert


class Route(StrEnum):
    SMALL_TALK = "SMALL_TALK"
    DIRECT_TOOL = "DIRECT_TOOL"
    TASK = "TASK"
    APPROVAL_REPLY = "APPROVAL_REPLY"
    CONNECT = "CONNECT"


class RouteDecision(BaseModel):
    route: Route
    reason: str = ""
    needs_clarification: str | None = Field(
        default=None, description="A question to ask instead of acting, if the request is ambiguous"
    )


class NotifyIntent(BaseModel):
    urgency: int = Field(ge=1, le=5)
    intent: str = Field(description="What the message should accomplish, not the wording")
    dedupe_key: str | None = None
    # Set only by deterministic code (the email security floor); hidden from the model's schema and
    # reset on model output. A security notice is never dropped by the daily budget.
    security: SkipJsonSchema[bool] = False


class TaskRequest(BaseModel):
    goal: str
    context: str = ""
    notify_on_complete: bool = True


class WakeupRequest(BaseModel):
    at: datetime
    reason: str
    loop_id: int | None = None


class InitiativeDecision(BaseModel):
    reasoning: str = ""
    notify: NotifyIntent | None = None
    act: list[TaskRequest] = Field(default_factory=list)
    track: list[LoopUpsert] = Field(default_factory=list)
    wakeups: list[WakeupRequest] = Field(default_factory=list)
    ignore_reason: str | None = None
    # Set by the reasoner's code, never the model: its prompt carried untrusted content (the signal, a
    # loop, recalled memory or a history message derived from third-party content).
    tainted: SkipJsonSchema[bool] = False


class ComposedMessage(BaseModel):
    send: bool = Field(description="False if no longer relevant given recent context")
    messages: list[str] = Field(default_factory=list, description="1-3 short chat bubbles")

