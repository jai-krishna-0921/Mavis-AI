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
from mavis.domain.events import Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopUpsert
from mavis.domain.messages import Role
from mavis.domain.timefmt import due_label
from mavis.domain.wakeups import WakeupKind, WakeupStatus
from mavis.initiative.executor import InitiativeExecutor
from mavis.initiative.untrusted import wrap_untrusted
from mavis.loops.service import LoopService
from mavis.policy import outcomes
from mavis.store.db import Session
from mavis.store.models import Message
from mavis.store.repo import users
from mavis.timers.service import WakeupService

log = structlog.get_logger()

MORNING_ROUTINE = "morning_checkin"
MORNING_TITLE = "Morning check-in"
EARLIEST, LATEST = 7 * 60, 11 * 60  # learned check-in clamped to 07:00–11:00 local
LEAD_MINUTES = 30                   # check in shortly before the user usually shows up
MIN_SAMPLES = 2                     # mornings needed (per weekday/weekend group) before trusting the data
MORNING_FROM, MORNING_UNTIL = 5 * 60, 12 * 60  # only first messages in 05:00 to 11:59 local count
MAX_IGNORED = 3  # after three check-ins with no user reply, stop sending until they write again


@dataclass(frozen=True)
class BriefItem:
    """One line of the morning brief. `trusted=False` for anything derived from third-party content."""

    text: str
    trusted: bool


class BriefSource(Protocol):
    """A source may also define `async delivered(user_id, at)`: called once the brief carrying its items
    went out (not when it was blocked or deferred), so "since the last brief" state is stamped only then.
    `at` is when the items were gathered. A brief sent later (from a quiet-hours deferral, or recovered
    after a crash) does not stamp, so its items may repeat in the next brief: by design, never lost."""

    name: str

    async def items(self, user_id: int, start: datetime, end: datetime) -> list[BriefItem]: ...


_sources: list[BriefSource] = []


def register_brief_source(src: BriefSource) -> None:
    _sources.append(src)


def clear_brief_sources() -> None:
    _sources.clear()


def unregister_brief_source(name: str) -> None:
    """Drop a source by name (the attention layer replaces Phase 5's live-search inbox source)."""
    _sources[:] = [s for s in _sources if getattr(s, "name", None) != name]


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


async def _tell_delivered(sources: list[BriefSource], user_id: int, at: datetime) -> None:
    for src in sources:
        hook = getattr(src, "delivered", None)
        if hook is None:
            continue
        try:
            await hook(user_id, at)
        except Exception:  # noqa: BLE001 - the brief is out; a failed stamp only repeats lines tomorrow
            log.exception("routines.brief_delivered_failed", source=getattr(src, "name", "?"))


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
                                                            importance=2, source="onboarding",
                                                            trust=Trust.SYSTEM, origin=LoopOrigin.ROUTINE))
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

    async def on_timezone_change(self, user_id: int, old: str, new: str) -> None:
        """The user moved: the morning check-in is re-anchored to their new local time. One-off reminders keep
        their absolute instant (they are stored in UTC and untouched here)."""
        user = await users.get(user_id)
        morning = [lp for lp in await self._loops.active(user_id)
                   if lp.kind is LoopKind.ROUTINE and lp.title == MORNING_TITLE]
        if not morning:
            return
        now = timeutil.now()
        recent = await self._wakeups.history(user_id, WakeupKind.ROUTINE, now - timedelta(days=2))
        fired_today = [w for w in recent
                       if w.status is WakeupStatus.FIRED and now - timedelta(hours=20) < w.due_at <= now]
        await self._wakeups.cancel_where(user_id, [WakeupKind.ROUTINE], morning[0].id)
        # a check-in that already went out in the old zone is not repeated in the new one the same day
        await self._schedule_morning(user, morning[0].id, next_day=bool(fired_today))

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
            BriefItem(f"{lp.title}, {due_label(lp.due_at, timeutil.now(), user.timezone)}", lp.trusted)
            for lp in await self._loops.active(user.id)
            if lp.kind is not LoopKind.ROUTINE and lp.due_at is not None
            and start.astimezone(UTC) <= lp.due_at < end.astimezone(UTC)
        ]
        # Recently failed actions and tasks come first: they need the user's decision (hotfix4 H1). The
        # same computed list the `pending` tool shows, so the brief never says a failed thing is set.
        try:
            failed = [BriefItem(f"{outcomes.HEADING}: {i.summary}: {i.outcome}, {i.reason}", not i.tainted)
                      for i in await outcomes.recently_failed(user.id)]
        except Exception:  # noqa: BLE001 - like a broken source, it must not kill the brief
            log.exception("routines.recently_failed_failed")
            failed = []
        items = failed + items
        gathered_at = timeutil.now()
        served: list[BriefSource] = []
        for src in list(_sources):
            try:
                for item in await src.items(user.id, start.astimezone(UTC), end.astimezone(UTC)):
                    if isinstance(item, str):  # legacy source without a trust marker: assume untrusted
                        item = BriefItem(item, False)
                    items.append(item)
                served.append(src)
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
        sent = await self._executor.notify(user, NotifyIntent(urgency=3, intent=intent, dedupe_key=key),
                                           untrusted=untrusted)
        if sent:
            await _tell_delivered(served, user.id, gathered_at)

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
        # Each day's earliest MORNING message: an evening-only day says nothing about when they start
        # their day, and a message just past midnight belongs to the night before.
        firsts: dict[date, datetime] = {}
        for ts in stamps:
            local = timeutil.to_local(ts, user.timezone)
            if not MORNING_FROM <= local.hour * 60 + local.minute < MORNING_UNTIL:
                continue
            if local.date() not in firsts or local < firsts[local.date()]:
                firsts[local.date()] = local
        minutes = [f.hour * 60 + f.minute for d, f in firsts.items() if (d.weekday() >= 5) == weekend]
        if len(minutes) < MIN_SAMPLES:
            return default
        target = max(EARLIEST, min(LATEST, int(statistics.median(minutes)) - LEAD_MINUTES))
        return time(target // 60, target % 60)
