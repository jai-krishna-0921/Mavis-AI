from __future__ import annotations

import re
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
    SYSTEM_ATTENTION_DRAIN = "system_attention_drain"  # Phase 8
    SYSTEM_ATTENTION_SPEAK = "system_attention_speak"  # Phase 8
    SYSTEM_ATTENTION_BACKFILL = "system_attn_backfill"  # Phase 8
    SYSTEM_EVENING_WRAP = "system_evening_wrap"  # Phase 8
    SYSTEM_ATTENTION_RETENTION = "system_attn_retention"  # Phase 8 (kind column is 24 chars)
    SYSTEM_LEARN = "system_learn"  # a LEARN job parked until after the chat grace window, or retried
    SYSTEM_WORKSPACE_POLL = "system_workspace_poll"  # Phase 9: Tasks/Drive polls and deferred Workspace pings
    SYSTEM_ONBOARD_CONNECT = "system_onboard_connect"  # Phase 11: the connector offer after the first turn
    SYSTEM_PERSONAL_LAYER = "system_personal_layer"  # rebuild a user's personal layer (kind column: 24 chars)


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


REMINDER_PREFIX = "Reminder the user asked for: "  # the reason prefix of a wake_me (user-requested) wakeup

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
    WakeupKind.SYSTEM_ATTENTION_DRAIN: EventType.WAKEUP,
    WakeupKind.SYSTEM_ATTENTION_SPEAK: EventType.WAKEUP,
    WakeupKind.SYSTEM_ATTENTION_BACKFILL: EventType.WAKEUP,
    WakeupKind.SYSTEM_EVENING_WRAP: EventType.WAKEUP,
    WakeupKind.SYSTEM_ATTENTION_RETENTION: EventType.WAKEUP,
    WakeupKind.SYSTEM_LEARN: EventType.WAKEUP,
    WakeupKind.SYSTEM_WORKSPACE_POLL: EventType.WAKEUP,
    WakeupKind.SYSTEM_ONBOARD_CONNECT: EventType.WAKEUP,
    WakeupKind.SYSTEM_PERSONAL_LAYER: EventType.WAKEUP,
}


def clean_what(text: str, name: str | None = None, *, capitalise: bool = False) -> str:
    """A reminder or loop title as the thing to do, in the user's own voice ("Stretch", "Call mom").

    The tool schema asks the model for exactly that; this is the safety net for what still slips through
    (and for titles stored before the schema said so): a leading "remind me to" / "remind <name> to" and
    a leading third-person subject ("<name> should", "<name> to") are dropped, whitespace is collapsed.
    Text with none of those is returned unchanged (`capitalise` only uppercases its first letter)."""
    what = " ".join(text.split())
    names = [re.escape(t) for t in (name or "").split() if len(t) > 1]
    me = "|".join(["me", "us", "myself", "you", *names])
    person = rf"(?:{me})(?:\s+(?:{me}))*"  # "me", "Jai", "Jai Krishna"
    link = r"(?:to|about|that|of|:)"
    wrapper = re.compile(rf"^(?:please\s+)?remind(?:er)?s?\b\s*(?:{person}\b\s*{link}?|{link})\s+",
                         re.IGNORECASE)
    own = "|".join(names)
    subject = (re.compile(rf"^(?:{own})(?:\s+(?:{own}))*\b\s*,?\s+"
                          r"(?:should\s+|needs?\s+to\s+|has\s+to\s+|to\s+)", re.IGNORECASE)
               if names else None)
    for pattern in (wrapper, subject):
        if pattern is not None and (m := pattern.match(what)) and what[m.end():].strip():
            what = what[m.end():].strip()
    if capitalise and what:
        what = what[0].upper() + what[1:]
    return what
