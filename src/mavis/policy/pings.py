"""Whether an unsolicited message may go out now (spec §8.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.messages import Role
from mavis.domain.policy import PolicyVerdict
from mavis.store.db import Session
from mavis.store.models import Message, PingLogRow
from mavis.store.repo import messages as messages_repo

URGENT = 5


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """True if `hour` is inside [start, end), wrapping midnight when start > end."""
    if start == end:
        return False
    return (hour >= start or hour < end) if start > end else (start <= hour < end)


def next_quiet_end(local_now: datetime, end_hour: int) -> datetime:
    """The next local `end_hour`:00 strictly after `local_now` (aware, same zone)."""
    candidate = local_now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    # compare as instants: same-zone aware comparison ignores offsets (DST folds)
    if candidate.astimezone(UTC) > local_now.astimezone(UTC):
        return candidate
    return candidate + timedelta(days=1)  # wall-clock arithmetic, DST-safe


def _local_midnight(local: datetime) -> datetime:
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _day_key(dedupe_key: str, local_now: datetime) -> str:
    return f"{dedupe_key}:{local_now.date().isoformat()}"


class PingPolicy:
    async def check(self, user, urgency: int, dedupe_key: str | None, now: datetime) -> PolicyVerdict:
        s = get_settings()
        local = timeutil.to_local(now, user.timezone)
        if dedupe_key and await self._seen(user.id, _day_key(dedupe_key, local)):
            return PolicyVerdict(allow=False, reason="duplicate")
        urgent = urgency >= URGENT  # urgent bypasses quiet hours only, never the daily budget (spec 8.4)
        if not urgent and in_quiet_hours(local.hour, s.quiet_start, s.quiet_end) and not await self._awake(
            user.id, now, s.quiet_awake_window_min
        ):
            defer = next_quiet_end(local, s.quiet_end).astimezone(UTC)
            return PolicyVerdict(allow=False, defer_until=defer, reason="quiet hours")
        if await self.count_today(user, now) >= s.ping_daily_budget:
            tomorrow = (_local_midnight(local) + timedelta(days=1)).replace(hour=s.quiet_end)
            return PolicyVerdict(
                allow=False, defer_until=tomorrow.astimezone(UTC), reason="daily budget reached"
            )
        return PolicyVerdict(allow=True)

    async def _awake(self, user_id: int, now: datetime, window_min: int) -> bool:
        """The user wrote recently, so they are up: quiet hours do not apply (budget and dedupe still do)."""
        if window_min <= 0:
            return False
        last = await messages_repo.last_user_message_at(user_id)
        if last is None:
            return False
        return timedelta(0) <= timeutil.ensure_utc(now) - last < timedelta(minutes=window_min)

    async def count_today(self, user, now: datetime) -> int:
        """Proactive assistant messages sent during the user's current local day."""
        local = timeutil.to_local(now, user.timezone)
        day_start = _local_midnight(local)
        day_end = day_start + timedelta(days=1)
        async with Session() as session:
            count = await session.scalar(
                select(func.count(Message.id)).where(
                    Message.user_id == user.id,
                    Message.role == Role.ASSISTANT.value,
                    Message.proactive.is_(True),
                    Message.created_at >= day_start.astimezone(UTC),
                    Message.created_at < day_end.astimezone(UTC),
                )
            )
        return int(count or 0)

    async def record(self, user, dedupe_key: str | None, urgency: int, now: datetime) -> None:
        if not dedupe_key:
            return
        key = _day_key(dedupe_key, timeutil.to_local(now, user.timezone))
        async with Session() as session:
            session.add(
                PingLogRow(user_id=user.id, key=key, urgency=urgency, sent_at=timeutil.ensure_utc(now))
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()  # already recorded: dedupe is the point

    async def _seen(self, user_id: int, key: str) -> bool:
        async with Session() as session:
            found = await session.scalar(
                select(PingLogRow.id).where(PingLogRow.user_id == user_id, PingLogRow.key == key)
            )
        return found is not None
