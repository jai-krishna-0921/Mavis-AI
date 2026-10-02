"""Notice when the user leaves Mavis hanging on a question (USER_QUIET)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.store.db import Session
from mavis.store.models import Message
from mavis.store.repo import messages, users
from mavis.timers.service import WakeupService

MAX_QUIET_STREAK = 2  # at most two unanswered nudges in a row
ONBOARDING_DAYS = 3   # nudges only while the user is new (spec 4.5)


def ends_with_question(text: str) -> bool:
    return "?" in text.strip()[-6:]


class QuietTracker:
    def __init__(self, wakeups: WakeupService) -> None:
        self._wakeups = wakeups

    async def after_assistant_message(self, user_id: int, text: str, streak: int = 0) -> int | None:
        await self._wakeups.cancel_where(user_id, [WakeupKind.USER_QUIET])  # newest question supersedes
        if not ends_with_question(text) or streak >= MAX_QUIET_STREAK:
            return None
        if not await self.in_onboarding(user_id):
            return None
        now = timeutil.now()
        return await self._wakeups.wake_me(
            user_id,
            now + timedelta(hours=get_settings().onboarding_quiet_hours),
            f"No reply to: {text[-120:]}",
            kind=WakeupKind.USER_QUIET,
            payload={"asked_at": now.isoformat(), "question": text[-300:], "streak": streak},
        )

    async def in_onboarding(self, user_id: int) -> bool:
        """True while the user is not onboarded or within ONBOARDING_DAYS of their first message."""
        user = await users.get(user_id)
        if not user.onboarded:
            return True
        async with Session() as s:
            first = await s.scalar(select(func.min(Message.created_at)).where(
                Message.user_id == user_id, Message.role == Role.USER.value))
        if first is None:
            return True
        return timeutil.now() - timeutil.ensure_utc(first) < timedelta(days=ONBOARDING_DAYS)

    async def on_user_message(self, user_id: int) -> int:
        return await self._wakeups.cancel_where(user_id, [WakeupKind.USER_QUIET])

    async def still_quiet(self, user_id: int, asked_at: datetime) -> bool:
        asked_at = timeutil.ensure_utc(asked_at)
        return not await messages.has_user_message_since(user_id, asked_at)
