from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from mavis.domain.events import Trust


class LoopKind(StrEnum):
    COMMITMENT = "COMMITMENT"
    WAITING_ON = "WAITING_ON"
    GOAL = "GOAL"
    CONCERN = "CONCERN"
    ROUTINE = "ROUTINE"
    WATCH = "WATCH"


class LoopStatus(StrEnum):
    OPEN = "OPEN"
    AWAITING_REPLY = "AWAITING"  # follow-up sent, waiting for the user's answer (closes on reply or 24h)
    DONE = "DONE"
    EXPIRED = "EXPIRED"
    DROPPED = "DROPPED"
    # Its action failed (an approval in the turn that created it): not OPEN, so no prep or "how did it
    # go" fires; the failure itself is shown as "recently failed" for the user to decide (hotfix4 H1).
    BLOCKED = "BLOCKED"


class LoopOrigin(StrEnum):
    """Which writer created a loop. Set by code from the writer, never inferred from `source`."""

    CONVERSATION = "conversation"  # extracted from, or tracked during, a chat turn
    REASONER = "reasoner"          # the initiative reasoner's `track`
    FEEDBACK = "feedback"          # a user's button press (attention dispute)
    ROUTINE = "routine"            # seeded by Mavis itself (morning check-in)
    UNKNOWN = "unknown"            # rows written before provenance was recorded


def least_trusted(a: Trust, b: Trust) -> Trust:
    """Taint is sticky: combining anything with untrusted content yields untrusted content."""
    if Trust.UNTRUSTED in (a, b):
        return Trust.UNTRUSTED
    return a


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
    source: str = ""                     # reference only (an event id); never read as trust
    version: int = 1                     # bumped on every change; keys LOOP_UPDATED event ids
    # Provenance: UNTRUSTED when any defining content came from a turn/run that saw third-party content.
    trust: Trust = Trust.UNTRUSTED
    origin: LoopOrigin = LoopOrigin.UNKNOWN
    created_at: datetime | None = None   # when the loop was first written (its title's anchor)
    created_ref: str | None = None       # the source it was created from (never changed by a merge)
    blocked_by: str | None = None        # while BLOCKED: the failed approval ref that blocked it

    @property
    def trusted(self) -> bool:
        return self.trust is not Trust.UNTRUSTED


class LoopUpsert(BaseModel):
    """Create a loop (id unset: kind and title required) or update one by id. An update is partial:
    only the fields the writer actually set change; the rest stay as they are."""

    id: int | None = None                # set to update an existing loop
    kind: LoopKind | None = None
    title: str | None = None
    due_at: datetime | None = None
    entities: list[str] = Field(default_factory=list)
    status: LoopStatus = LoopStatus.OPEN
    importance: int = Field(ge=1, le=5, default=3)
    watch: WatchSpec | None = None
    source: str = ""
    # Set by the writing code from the origin's trust, hidden from (and reset on) model output. The
    # default is the safe one: a writer that forgets to say yields an untrusted loop.
    trust: SkipJsonSchema[Trust] = Trust.UNTRUSTED
    origin: SkipJsonSchema[LoopOrigin] = LoopOrigin.UNKNOWN

    @model_validator(mode="after")
    def _new_loop_is_complete(self) -> LoopUpsert:
        if self.id is None and (self.kind is None or not (self.title or "").strip()):
            raise ValueError("a new loop needs a kind and a title")
        return self
