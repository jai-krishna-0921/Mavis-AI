"""Notice when the user leaves Mavis hanging on a question (USER_QUIET)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.loops import LoopKind, LoopOrigin
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.store.db import Session
from mavis.store.models import Message
from mavis.store.repo import messages, users
from mavis.timers.service import WakeupService

MAX_QUIET_STREAK = 2  # at most two unanswered nudges in a row
ONBOARDING_DAYS = 3   # onboarding nudges only while the user is new (spec 4.5); then onboarded is set


def ends_with_question(text: str) -> bool:
    return "?" in text.strip()[-6:]


class QuietTracker:
    def __init__(self, wakeups: WakeupService) -> None:
        self._wakeups = wakeups

    async def after_assistant_message(self, user_id: int, text: str, streak: int = 0) -> int | None:
        """Arm a USER_QUIET nudge only for the onboarding question of a new user who has given Mavis
        nothing to track yet (spec 4.5). After that nothing is chased: a chat question cannot be tied to
        a user item structurally, Mavis's optional offers ("want me to...?") are never chased, and
        proactive questions (a "how did it go?" follow-up) never arm a nudge (QA F3)."""
        await self._wakeups.cancel_where(user_id, [WakeupKind.USER_QUIET])  # newest question supersedes
        if not ends_with_question(text) or streak >= MAX_QUIET_STREAK:
            return None
        if not await self.owed(user_id):
            return None
        now = timeutil.now()
        payload = {"asked_at": now.isoformat(), "question": text[-300:], "streak": streak}
        return await self._wakeups.wake_me(
            user_id,
            now + timedelta(hours=get_settings().onboarding_quiet_hours),
            f"No reply to: {text[-120:]}",
            kind=WakeupKind.USER_QUIET,
            payload=payload,
        )

    async def owed(self, user_id: int) -> bool:
        """Is an onboarding nudge still owed? Checked when arming and again when it fires."""
        return await self.in_onboarding(user_id) and not await _has_own_items(user_id)

    async def in_onboarding(self, user_id: int) -> bool:
        """True within ONBOARDING_DAYS of the user's first message. When the window has passed the user
        is marked onboarded (nothing else sets the flag), so the check is one read afterwards."""
        user = await users.get(user_id)
        if user.onboarded:
            return False
        async with Session() as s:
            first = await s.scalar(select(func.min(Message.created_at)).where(
                Message.user_id == user_id, Message.role == Role.USER.value))
        if first is None:
            return True
        if timeutil.now() - timeutil.ensure_utc(first) < timedelta(days=ONBOARDING_DAYS):
            return True
        await users.update(user_id, onboarded=True)
        return False

    async def on_user_message(self, user_id: int) -> int:
        return await self._wakeups.cancel_where(user_id, [WakeupKind.USER_QUIET])

    async def still_quiet(self, user_id: int, asked_at: datetime) -> bool:
        asked_at = timeutil.ensure_utc(asked_at)
        return not await messages.has_user_message_since(user_id, asked_at)


async def _has_own_items(user_id: int) -> bool:
    """Live items that are the user's (not routines, not the reasoner's own beliefs)."""
    from mavis.store.repo import loops

    return any(lp.kind is not LoopKind.ROUTINE and lp.origin is not LoopOrigin.REASONER
               for lp in await loops.list_live(user_id))
