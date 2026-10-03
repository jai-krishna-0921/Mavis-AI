"""Morning brief section, evening wrap-up, first-look summary and retention (spec attention 11 and 13)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from mavis.attention.index import AttentionIndex
from mavis.attention.learning import Thresholds
from mavis.attention.scheduling import schedule_once
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.policy import Capability
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.routines import BriefItem
from mavis.initiative.untrusted import wrap_untrusted
from mavis.store.repo import attention as repo
from mavis.store.repo import users
from mavis.timers.service import WakeupService

log = structlog.get_logger()

BRIEF_LOOKBACK = timedelta(hours=18)
MAX_BRIEF = 5
WAITING = frozenset({"brief", "notify", "ask"})
ROUTINE = frozenset({"log", "dropped"})
EVENING_REASON = "evening"
EVENING_EARLIEST, EVENING_LATEST = 12, 23  # a wrap that fires outside this local window is skipped
PENDING_MAX_AGE = timedelta(days=2)
NOTHING = "Inbox: nothing new that needs you."
RETENTION_REASON = "retention"
RETENTION_TIME = "03:30"  # local: daily purge runs while the user sleeps and the LLM slot is idle

EveningSource = Callable[[int, datetime], Awaitable[list[str]]]  # (user_id, local midnight in UTC) -> lines
_evening_sources: list[EveningSource] = []


def register_evening_source(fn: EveningSource) -> None:
    """Extra evening-wrap lines from other sources (Workspace: overdue tasks, comments on the user's docs)."""
    if fn not in _evening_sources:
        _evening_sources.append(fn)


def clear_evening_sources() -> None:
    _evening_sources.clear()


def _waiting(rows: list[Any]) -> list[Any]:
    return [r for r in rows if r.verdict in WAITING and r.feedback is None]


def _line(r: Any) -> str:
    return r.summary + (f" (asks: {r.action})" if r.action else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class AttentionBrief:
    """Morning-brief source (routines.register_brief_source). Lines derive from email subjects: untrusted."""

    name = "attention"

    async def items(self, user_id: int, start: datetime, end: datetime) -> list[BriefItem]:
        rows = await repo.recent(user_id, timeutil.now() - BRIEF_LOOKBACK, origin=repo.ORIGIN_LIVE, limit=100)
        if not rows:  # a quiet night: say so, but only to users whose Gmail is actually watched
            polling = (await users.get_state(user_id)).get("polling") or {}
            return [BriefItem(NOTHING, True)] if polling.get(Capability.GMAIL.value) else []
        waiting = _waiting(rows)
        items = [BriefItem(f"Email: {_line(r)}", False) for r in waiting[:MAX_BRIEF]]
        if not waiting:
            items.append(BriefItem(NOTHING, True))
        routine = sum(1 for r in rows if r.verdict in ROUTINE)
        if routine:
            items.append(
                BriefItem(f"Inbox: {_plural(routine, 'routine email')} handled quietly overnight.", True)
            )
        return items


def next_local(now: datetime, tz: str, hhmm: str) -> datetime:
    """The next time the user's wall clock reads `hhmm` (strictly after `now`), in UTC."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    local = timeutil.to_local(now, tz)
    candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def next_evening(now: datetime, tz: str, hhmm: str) -> datetime:
    return next_local(now, tz, hhmm)


class EveningWrap:
    def __init__(self, executor_of: Callable[[], Any], wakeups: WakeupService) -> None:
        self._executor_of, self._wakeups = executor_of, wakeups

    async def ensure(self, user_id: int) -> None:
        s = get_settings()
        if not s.attention_evening_enabled:
            return
        user = await users.get(user_id)
        at = next_evening(timeutil.now(), user.timezone, s.attention_evening_time)
        await schedule_once(self._wakeups, user_id, WakeupKind.SYSTEM_EVENING_WRAP, EVENING_REASON, at)

    async def run(self, user_id: int, reason: str = "") -> None:
        """system_evening_wrap: always books tomorrow's, even if sending failed."""
        try:
            await self._send(user_id)
        finally:
            await self.ensure(user_id)

    async def _send(self, user_id: int) -> bool:
        user = await users.get(user_id)
        local = timeutil.to_local(timeutil.now(), user.timezone)
        if not EVENING_EARLIEST <= local.hour < EVENING_LATEST:
            log.info("attention.evening_skipped", user_id=user_id, reason="outside window")
            return False
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        rows = await repo.recent(user_id, start, origin=repo.ORIGIN_LIVE, limit=200)
        waiting = _waiting(rows)
        flagged = [r for r in rows if r.verdict in ("notify", "ask")]
        extra: list[str] = []
        for source in list(_evening_sources):
            try:
                extra += await source(user_id, start)
            except Exception as exc:  # noqa: BLE001 - the inbox wrap still goes out
                log.warning("attention.evening_source_failed", error=type(exc).__name__)
        if not waiting and not flagged and not extra:
            log.info("attention.evening_skipped", user_id=user_id, reason="nothing notable")
            return False
        handled = sum(1 for r in rows if r.verdict in ROUTINE)
        parts = [
            "Short evening wrap-up of today's inbox, one or two bubbles, no greeting. "
            f"You handled {_plural(handled, 'email')} quietly today."
        ]
        if flagged:
            parts.append(f"You flagged {len(flagged)} to them earlier.")
        if waiting:
            lines = "\n".join(f"- {_line(r)}" for r in waiting[:MAX_BRIEF])
            parts.append(
                f"Still waiting on them:\n{wrap_untrusted(lines, 'evening')}\nOffer to help with one."
            )
        else:
            parts.append("Nothing is waiting on them now.")
        if extra:
            lines = "\n".join(f"- {x}" for x in extra[:MAX_BRIEF])
            parts.append(f"From their Google account:\n{wrap_untrusted(lines, 'evening_google')}")
        intent = NotifyIntent(
            urgency=2, intent="\n".join(parts), dedupe_key=f"evening:{local.date().isoformat()}"
        )
        # Quiet hours defer via the executor; a wrap-up deferred past tonight is stale, never a morning ping.
        valid_until = local.replace(hour=EVENING_LATEST, minute=0, second=0, microsecond=0).astimezone(UTC)
        origin = {"kind": "evening_wrap", "valid_until": valid_until.isoformat()}
        return await self._executor_of().notify(user, intent, untrusted=bool(waiting or extra), origin=origin)


class FirstLook:
    """After the Gmail backfill is understood: one summary of the last two weeks, only if worth saying."""

    def __init__(self, executor_of: Callable[[], Any], thresholds: Thresholds) -> None:
        self._executor_of, self._thresholds = executor_of, thresholds

    async def maybe_send(self, user: Any) -> bool:
        state = await self._thresholds.load(user.id)
        if not state.get("backfilled_at") or state.get("first_look_at"):
            return False
        await self._thresholds.patch(user.id, first_look_at=timeutil.now().isoformat())
        rows = await repo.recent(
            user.id, timeutil.now() - timedelta(days=15), origin=repo.ORIGIN_BACKFILL, limit=200
        )
        notable = [r for r in rows if r.verdict in WAITING]
        payments = sum(1 for r in rows if ((r.facts or {}).get("money") or {}).get("direction") == "debit")
        if not notable and payments < 3:
            return False
        parts = [
            "Tell the user, warmly and briefly, that you finished reading their last two weeks of email "
            f"({_plural(len(rows), 'email')})."
        ]
        if payments:
            parts.append(
                f"You learned their usual spending from {_plural(payments, 'payment')}, so you can now "
                "spot unusual ones and check with them."
            )
        if notable:
            lines = "\n".join(f"- {_line(r)}" for r in notable[:4])
            wrapped = wrap_untrusted(lines, "first_look")
            parts.append(f"Things from those two weeks that may still need them:\n{wrapped}")
        else:
            parts.append("Nothing from those two weeks still needs them.")
        intent = NotifyIntent(urgency=3, intent="\n".join(parts), dedupe_key=f"attn:firstlook:{user.id}")
        return await self._executor_of().notify(user, intent, untrusted=bool(notable))


async def purge(index: AttentionIndex, user_id: int | None = None) -> int:
    """Retention: observations older than ATTENTION_RETENTION_DAYS and their vectors; stale snippets.
    Scoped to one user when `user_id` is given (the per-user retention wakeup)."""
    now = timeutil.now()
    expired = await repo.expire_pending(now - PENDING_MAX_AGE, user_id)
    points = await repo.purge_before(now - timedelta(days=get_settings().attention_retention_days), user_id)
    try:
        await index.delete(points)
    except Exception as exc:  # noqa: BLE001 - rows are gone; orphan vectors are harmless and retried never
        log.warning("attention.retention_index_failed", error=type(exc).__name__)
    log.info("attention.retention", user_id=user_id, purged=len(points), expired=expired)
    return len(points)


class Retention:
    """A self-rescheduling daily system wakeup per user: expire stale pending mail, purge past retention."""

    def __init__(self, index_of: Callable[[], AttentionIndex], wakeups: WakeupService) -> None:
        self._index_of, self._wakeups = index_of, wakeups

    async def ensure(self, user_id: int) -> None:
        user = await users.get(user_id)
        at = next_local(timeutil.now(), user.timezone, RETENTION_TIME)
        await schedule_once(
            self._wakeups, user_id, WakeupKind.SYSTEM_ATTENTION_RETENTION, RETENTION_REASON, at
        )

    async def run(self, user_id: int, reason: str = "") -> None:
        """system_attn_retention: always books tomorrow's run, even if this one failed."""
        try:
            await purge(self._index_of(), user_id)
        finally:
            await self.ensure(user_id)
