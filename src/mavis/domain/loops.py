from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class LoopKind(StrEnum):
    COMMITMENT = "COMMITMENT"
    WAITING_ON = "WAITING_ON"
    GOAL = "GOAL"
    CONCERN = "CONCERN"
    ROUTINE = "ROUTINE"
    WATCH = "WATCH"


class LoopStatus(StrEnum):
    OPEN = "OPEN"
    DONE = "DONE"
    EXPIRED = "EXPIRED"
    DROPPED = "DROPPED"


class WatchSpec(BaseModel):
    from_contains: str | None = None     # sender email/name substring
    thread_id: str | None = None
    keywords: list[str] = Field(default_factory=list)
    deadline: datetime | None = None


class Loop(BaseModel):
    id: int
    user_id: int
    kind: LoopKind
    title: str
    due_at: datetime | None = None
    entities: list[str] = Field(default_factory=list)
    status: LoopStatus = LoopStatus.OPEN
    importance: int = 3
    watch: WatchSpec | None = None
    source: str = ""
    version: int = 1                     # bumped on every change; keys LOOP_UPDATED event ids


class LoopUpsert(BaseModel):
    id: int | None = None                # set to update an existing loop
    kind: LoopKind
    title: str
    due_at: datetime | None = None
    entities: list[str] = Field(default_factory=list)
    status: LoopStatus = LoopStatus.OPEN
    importance: int = Field(ge=1, le=5, default=3)
    watch: WatchSpec | None = None
    source: str = ""
