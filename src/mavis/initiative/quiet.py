"""Notice when the user leaves Mavis hanging on a question (USER_QUIET)."""

from __future__ import annotations

from datetime import datetime, timedelta

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.store.repo import messages
from mavis.timers.service import WakeupService

MAX_QUIET_STREAK = 2  # at most two unanswered nudges in a row


def ends_with_question(text: str) -> bool:
    return "?" in text.strip()[-6:]


class QuietTracker:
    def __init__(self, wakeups: WakeupService) -> None:
        self._wakeups = wakeups

    async def after_assistant_message(self, user_id: int, text: str, streak: int = 0) -> int | None:
        await self._wakeups.cancel_where(user_id, [WakeupKind.USER_QUIET])  # newest question supersedes
        if not ends_with_question(text) or streak >= MAX_QUIET_STREAK:
            return None
        now = timeutil.now()
        return await self._wakeups.wake_me(
            user_id,
            now + timedelta(hours=get_settings().onboarding_quiet_hours),
            f"No reply to: {text[-120:]}",
            kind=WakeupKind.USER_QUIET,
            payload={"asked_at": now.isoformat(), "question": text[-300:], "streak": streak},
        )

    async def on_user_message(self, user_id: int) -> int:
        return await self._wakeups.cancel_where(user_id, [WakeupKind.USER_QUIET])

    async def still_quiet(self, user_id: int, asked_at: datetime) -> bool:
        asked_at = timeutil.ensure_utc(asked_at)
        return not any(
            m.role == Role.USER and timeutil.ensure_utc(m.created_at) > asked_at
            for m in await messages.recent(user_id, 10)
        )
