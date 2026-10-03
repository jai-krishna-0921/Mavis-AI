"""Task-board and approval vocabulary shared by store, agents and policy."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskKind(StrEnum):
    TASK = "task"  # planned multi-agent work
    APPROVAL = "approval"  # only carries approvals queued during a chat turn


class TaskOrigin(StrEnum):
    USER = "user"
    INITIATIVE = "initiative"


class ApprovalStatus(StrEnum):
    PENDING = "pending"  # buttons shown, waiting for the user
    AWAITING_EDIT = "awaiting_edit"  # user tapped Edit, waiting for instructions
    RESOLVING = "resolving"  # decision received, resume queued
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


OPEN_APPROVAL_STATUSES = frozenset(
    {ApprovalStatus.PENDING, ApprovalStatus.AWAITING_EDIT, ApprovalStatus.RESOLVING}
)
TERMINAL_APPROVAL_STATUSES = frozenset(
    {ApprovalStatus.EXECUTED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.FAILED}
)


class StepOutcome(BaseModel):
    """What one specialist / spawned worker produced for one plan step."""

    ok: bool
    text: str = ""
    artifacts: list[str] = Field(default_factory=list)  # local file paths
    error: str | None = None
