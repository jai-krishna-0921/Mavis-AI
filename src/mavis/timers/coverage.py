"""Who owns a moment: a reminder the user asked for covers anything else that would speak about the same
thing at the same time. One intent from the user gives one proactive message.

The live E2E produced three messages for one "remind me in 3 minutes to drink water" (the wake_me, a loop
that LEARN extracted from the same sentence, and a reasoner wakeup for that loop) and five wakeups for one
focus block. The identity is (when, what): a wakeup or loop within COVER_WINDOW of a pending user reminder
that names the same matter (store.repo.loops.same_matter) is a restatement, not a new thing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mavis.domain import timeutil
from mavis.domain.wakeups import REMINDER_PREFIX, Wakeup
from mavis.store.repo import wakeups as repo
from mavis.store.repo.loops import same_matter

COVER_WINDOW = timedelta(minutes=20)
SAME_REMINDER_WINDOW = timedelta(minutes=2)


def reminder_matter(w: Wakeup) -> str:
    return w.reason.removeprefix(REMINDER_PREFIX).strip()


def is_user_reminder(w: Wakeup) -> bool:
    return bool(w.payload.get("reminder"))


async def covering_reminder(user_id: int, texts: list[str], at: datetime,
                            window: timedelta = COVER_WINDOW) -> Wakeup | None:
    """A pending reminder the user asked for, due within `window` of `at`, about the same matter as any of
    `texts` (a loop title, a wakeup reason)."""
    at = timeutil.ensure_utc(at)
    for w in await repo.list_pending(user_id):
        if is_user_reminder(w) and abs(w.due_at - at) <= window and \
                any(same_matter(reminder_matter(w), text) for text in texts if text):
            return w
    return None
