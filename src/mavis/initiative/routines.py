"""Learned routines: seeded once, then rescheduled by the engine itself (spec §4.5)."""

from __future__ import annotations

import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

import structlog
from sqlalchemy import func, select

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.loops import LoopKind, LoopUpsert
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.executor import InitiativeExecutor
from mavis.initiative.untrusted import wrap_untrusted
from mavis.loops.service import LoopService
from mavis.store.db import Session
from mavis.store.models import Message
from mavis.timers.service import WakeupService

log = structlog.get_logger()

MORNING_ROUTINE = "morning_checkin"
MORNING_TITLE = "Morning check-in"
EARLIEST, LATEST = 7 * 60, 11 * 60  # learned check-in clamped to 07:00–11:00 local
LEAD_MINUTES = 30                   # check in shortly before the user usually shows up
MIN_SAMPLES = 2
MAX_IGNORED = 3  # after three check-ins with no user reply, stop sending until they write again


@dataclass(frozen=True)
class BriefItem:
    """One line of the morning brief. `trusted=False` for anything derived from third-party content."""

    text: str
    trusted: bool


class BriefSource(Protocol):
    name: str

    async def items(self, user_id: int, start: datetime, end: datetime) -> list[BriefItem]: ...


_sources: list[BriefSource] = []


def register_brief_source(src: BriefSource) -> None:
    _sources.append(src)


def clear_brief_sources() -> None:
    _sources.clear()


def brief_sources() -> list[BriefSource]:
    return list(_sources)


MorningHook = Callable[[int], Awaitable[None]]
_morning_hooks: list[MorningHook] = []


def register_morning_hook(fn: MorningHook) -> None:
    """Low-frequency maintenance that rides on the daily check-in (e.g. re-arming poll chains)."""
    if fn not in _morning_hooks:
        _morning_hooks.append(fn)


def clear_morning_hooks() -> None:
    _morning_hooks.clear()


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


class Routines:
    def __init__(self, loops: LoopService, wakeups: WakeupService, executor: InitiativeExecutor) -> None:
        self._loops, self._wakeups, self._executor = loops, wakeups, executor

    async def on_user_message(self, user) -> None:
        """Seed routines the first time; cheap no-op afterwards."""
        existing = [lp for lp in await self._loops.active(user.id)
                    if lp.kind is LoopKind.ROUTINE and lp.title == MORNING_TITLE]
        if existing:
            # Idempotent self-heal: an existing user whose wakeup is missing gets it back.
            if not await self._wakeups.pending(user.id, WakeupKind.ROUTINE):
                await self._schedule_morning(user, existing[0].id, next_day=True)
            return
        loop = await self._loops.upsert(user.id, LoopUpsert(kind=LoopKind.ROUTINE, title=MORNING_TITLE,
                                                            importance=2, source="onboarding"))
        await self._schedule_morning(user, loop.id, next_day=True)

    async def run(self, user, payload: dict[str, Any]) -> None:
        if payload.get("routine") == MORNING_ROUTINE:
            await self.morning_checkin(user, payload.get("loop_id"))
        else:
            log.warning("routines.unknown", payload=payload)

    async def morning_checkin(self, user, loop_id: int | None) -> None:
        failed = False
        for hook in list(_morning_hooks):
            try:
                await hook(user.id)
            except Exception:  # noqa: BLE001 - maintenance must never block the check-in
                log.exception("routines.morning_hook_failed", user=user.id)
        try:
            await self._send_morning(user)
        except BaseException:
            failed = True
            log.exception("routines.morning_failed", user=user.id)
            raise
        finally:
            try:  # always reschedule, but never let a reschedule failure mask the original error
                await self._schedule_morning(user, loop_id, next_day=True)
            except Exception:
                if not failed:
                    raise
                log.exception("routines.reschedule_failed", user=user.id)

    async def reschedule(self, user, loop_id: int | None) -> None:
        """A check-in that fired far too late is skipped; the next one is booked for tomorrow."""
        await self._schedule_morning(user, loop_id, next_day=True)

    async def _send_morning(self, user) -> None:
        if await self._ignored_streak(user.id) >= MAX_IGNORED:
            log.info("routines.morning_skipped_ignored", user=user.id)
            return
        local_now = timeutil.to_local(timeutil.now(), user.timezone)
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        items = [
            BriefItem(f"{lp.title} at {timeutil.to_local(lp.due_at, user.timezone):%H:%M}", True)
            for lp in await self._loops.active(user.id)
            if lp.kind is not LoopKind.ROUTINE and lp.due_at is not None
            and start.astimezone(UTC) <= lp.due_at < end.astimezone(UTC)
        ]
        for src in list(_sources):
            try:
                for item in await src.items(user.id, start.astimezone(UTC), end.astimezone(UTC)):
                    if isinstance(item, str):  # legacy source without a trust marker: assume untrusted
                        item = BriefItem(item, False)
                    items.append(item)
            except Exception:  # noqa: BLE001 - one broken source must not kill the brief
                log.exception("routines.brief_source_failed", source=getattr(src, "name", "?"))
        untrusted = any(not i.trusted for i in items)
        if items:
            lines = [i.text if i.trusted else wrap_untrusted(i.text, "brief") for i in items]
            intent = "Warm good-morning check-in. Today's items:\n" + "\n".join(f"- {line}" for line in lines)
        else:
            intent = ("Warm good-morning check-in. Nothing scheduled today: ask what's on their plate "
                      "or nudge gently on one of their goals.")
        key = f"morning:{local_now.date().isoformat()}"
        await self._executor.notify(user, NotifyIntent(urgency=3, intent=intent, dedupe_key=key),
                                    untrusted=untrusted)

    async def _ignored_streak(self, user_id: int) -> int:
        async with Session() as s:
            last_user = await s.scalar(select(func.max(Message.created_at)).where(
                Message.user_id == user_id, Message.role == Role.USER.value))
            q = select(func.count()).select_from(Message).where(
                Message.user_id == user_id, Message.proactive.is_(True),
                Message.event_id.like("proactive:morning:%"))
            if last_user is not None:
                q = q.where(Message.created_at > last_user)
            return int(await s.scalar(q) or 0)

    async def _schedule_morning(self, user, loop_id: int | None, next_day: bool) -> int:
        at = await self.next_morning_time(user, next_day=next_day)
        local_day = timeutil.to_local(at, user.timezone).date().isoformat()
        return await self._wakeups.wake_me(
            user.id, at, MORNING_TITLE, loop_id, WakeupKind.ROUTINE,
            payload={"routine": MORNING_ROUTINE}, dedupe_key=f"morning:{local_day}", scale=False,
        )

    async def next_morning_time(self, user, next_day: bool = False) -> datetime:
        local_now = timeutil.to_local(timeutil.now(), user.timezone)
        day = local_now.date() + timedelta(days=1 if next_day else 0)
        candidate = local_now
        for _ in range(3):
            t = await self.learned_checkin_time(user, weekend=day.weekday() >= 5)
            candidate = datetime.combine(day, t, tzinfo=local_now.tzinfo)
            if candidate > local_now:
                break
            day += timedelta(days=1)
        return candidate.astimezone(UTC)

    async def learned_checkin_time(self, user, weekend: bool) -> time:
        default = _parse_hhmm(get_settings().morning_checkin_time)
        since = timeutil.now() - timedelta(days=7)
        async with Session() as s:
            stamps = list(await s.scalars(select(Message.created_at).where(
                Message.user_id == user.id, Message.role == Role.USER.value, Message.created_at >= since)))
        firsts: dict[date, datetime] = {}
        for ts in stamps:
            local = timeutil.to_local(ts, user.timezone)
            if local.date() not in firsts or local < firsts[local.date()]:
                firsts[local.date()] = local
        minutes = [f.hour * 60 + f.minute for d, f in firsts.items() if (d.weekday() >= 5) == weekend]
        if len(minutes) < MIN_SAMPLES:
            return default
        target = max(EARLIEST, min(LATEST, int(statistics.median(minutes)) - LEAD_MINUTES))
        return time(target // 60, target % 60)
