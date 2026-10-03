"""Re-arming system wakeups without collapsing onto the row that is firing right now."""

from __future__ import annotations

from datetime import datetime

from mavis.domain import timeutil
from mavis.domain.wakeups import WakeupKind
from mavis.timers.service import WakeupService


async def schedule_once(
    wakeups: WakeupService, user_id: int, kind: WakeupKind, reason: str, at: datetime
) -> int:
    """One pending wakeup per (kind, reason), without ever ending a chain.

    A request for "now" is absorbed by any pending row (it is due or about to be). A request for later is
    absorbed only by a row that is still in the future: the row being fired stays PENDING until the timer
    commits, and collapsing onto it would silently end a self-rescheduling chain (the Phase 5 poll lesson)."""
    now = timeutil.now()
    for w in await wakeups.pending(user_id, kind):
        if w.reason == reason and (w.due_at > now or at <= now):
            return w.id
    return await wakeups.wake_me(user_id, at, reason, kind=kind, scale=False)  # plumbing: never demo-scaled
