from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from mavis.domain.events import EventType


class WakeupKind(StrEnum):
    AGENT = "agent"                    # the initiative agent asked to look again
    ROUTINE = "routine"                # morning check-in and other learned routines
    USER_QUIET = "user_quiet"          # Mavis asked something and hasn't heard back
    EVENT_STARTING = "event_starting"  # prep / pep talk before a commitment
    EVENT_ENDED = "event_ended"        # "how did it go?" after a commitment
    DEFERRED = "deferred"              # a notification postponed by quiet hours / budget
    # System wakeups: plumbing owned by later phases, routed by mavis.timers.system, never reasoned about.
    SYSTEM_APPROVAL_REMIND = "system_approval_remind"    # Phase 4
    SYSTEM_APPROVAL_EXPIRE = "system_approval_expire"    # Phase 4
    SYSTEM_TASK_DELIVERY = "system_task_delivery"        # Phase 4
    SYSTEM_POLL = "system_poll"                          # Phase 5
    SYSTEM_CONNECTION_CHECK = "system_connection_check"  # Phase 5


class WakeupStatus(StrEnum):
    PENDING = "pending"
    FIRED = "fired"
    CANCELLED = "cancelled"


class Wakeup(BaseModel):
    id: int
    user_id: int
    due_at: datetime
    kind: WakeupKind
    reason: str
    loop_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: WakeupStatus = WakeupStatus.PENDING


# payload keys the timer sets itself; callers may not supply them. "reminder" (a user-requested reminder,
# which always fires and skips the daily budget) is set only through WakeupService.wake_me(reminder=True).
RESERVED_PAYLOAD_KEYS = frozenset({"wakeup_id", "kind", "reason", "loop_id", "reminder"})

EVENT_TYPE_FOR_KIND: dict[WakeupKind, EventType] = {
    WakeupKind.AGENT: EventType.WAKEUP,
    WakeupKind.ROUTINE: EventType.WAKEUP,
    WakeupKind.DEFERRED: EventType.WAKEUP,
    WakeupKind.USER_QUIET: EventType.USER_QUIET,
    WakeupKind.EVENT_STARTING: EventType.EVENT_STARTING,
    WakeupKind.EVENT_ENDED: EventType.EVENT_ENDED,
    WakeupKind.SYSTEM_APPROVAL_REMIND: EventType.WAKEUP,
    WakeupKind.SYSTEM_APPROVAL_EXPIRE: EventType.WAKEUP,
    WakeupKind.SYSTEM_TASK_DELIVERY: EventType.WAKEUP,
    WakeupKind.SYSTEM_POLL: EventType.WAKEUP,
    WakeupKind.SYSTEM_CONNECTION_CHECK: EventType.WAKEUP,
}
