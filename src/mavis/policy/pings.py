"""Whether an unsolicited message may go out now (spec §8.4)."""

from __future__ import annotations

import re
from collections.abc import Sequence
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


def _keys(dedupe_key: str | None, extra: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(k for k in (dedupe_key, *extra) if k))


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_dedupe_key(key: str | None) -> str | None:
    """Model-chosen keys drift ("thank_you_jawahar" vs "thankyou_jawahar"): compare them lowercased,
    alphanumerics only, without a date (keys are scoped per local day anyway)."""
    if not key:
        return None
    return _NON_ALNUM.sub("", _ISO_DATE.sub("", key.lower())) or None


def loop_ping_key(loop_id: int | str | None, kind: str | None) -> str | None:
    """One ping per loop and kind of ping per day, whatever key the model chose."""
    if loop_id in (None, "") or not kind:
        return None
    return f"loop:{loop_id}:{kind}"


def _day_key(dedupe_key: str, local_now: datetime) -> str:
    """Scope a key to the local day; keys that already carry today's date are not dated twice."""
    day = local_now.date().isoformat()
    if day in dedupe_key or day.replace("-", "") in dedupe_key:
        return dedupe_key
    return f"{dedupe_key}:{day}"


class PingPolicy:
    async def check(self, user, urgency: int, dedupe_key: str | None, now: datetime,
                    extra_keys: Sequence[str] = ()) -> PolicyVerdict:
        """`extra_keys` are further dedupe keys (e.g. one per loop and kind of ping): any seen one blocks."""
        s = get_settings()
        local = timeutil.to_local(now, user.timezone)
        for key in _keys(dedupe_key, extra_keys):
            if await self._seen(user.id, _day_key(key, local)):
                return PolicyVerdict(allow=False, reason="duplicate")
        urgent = urgency >= URGENT  # urgent bypasses quiet hours only, never the daily budget (spec 8.4)
        if not urgent and in_quiet_hours(local.hour, s.quiet_start, s.quiet_end) and not await self._awake(
            user.id, now, s.quiet_awake_window_min
        ):
            defer = next_quiet_end(local, s.quiet_end).astimezone(UTC)
            return PolicyVerdict(allow=False, defer_until=defer, reason="quiet hours")
        if await self.count_today(user, now) >= s.ping_daily_budget:
            if not urgent:  # a routine ping is stale by tomorrow: drop it rather than pile up a backlog
                return PolicyVerdict(allow=False, reason="daily budget reached")
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

    async def record(self, user, dedupe_key: str | None, urgency: int, now: datetime,
                     extra_keys: Sequence[str] = ()) -> None:
        local = timeutil.to_local(now, user.timezone)
        for raw in _keys(dedupe_key, extra_keys):
            key = _day_key(raw, local)
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
