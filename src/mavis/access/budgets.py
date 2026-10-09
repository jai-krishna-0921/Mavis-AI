"""Per-user daily budgets (spec 9.3), cooldowns and bans (spec 9.4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from enum import IntEnum

import structlog
from sqlalchemy import delete, update

from mavis import bus
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.messages import Outbound
from mavis.llm.usage import spend_micros_today
from mavis.store.db import Session, utcnow
from mavis.store.models import OutboxMessage, WakeupRow
from mavis.store.repo import audit, outbox, users

log = structlog.get_logger(__name__)
HARD_CAP_TEXT = ("I've hit today's limit for heavy work. Reminders and quick answers still work, and "
                 "everything resets at midnight your time.")
RUNAWAY_TEXT = "I've done a lot for you today, so I'm pausing until midnight your time."
SpendSource = Callable[[int, date], Awaitable[float]]
_sources: dict[str, SpendSource] = {}
_cool_mem: dict[int, float] = {}
_hits_mem: dict[int, list[float]] = {}


class BudgetState(IntEnum):
    OK = 0
    SOFT = 1
    HARD = 2
    RUNAWAY = 3


def register_spend_source(name: str, fn: SpendSource) -> None:
    _sources[name] = fn


async def spend_today_usd(user_id: int) -> float:
    total = (await spend_micros_today(user_id)) / 1_000_000
    tz = (await users.get(user_id)).timezone if user_id else get_settings().default_timezone
    day = timeutil.to_local(timeutil.now(), tz).date()
    for name, fn in list(_sources.items()):
        try:
            total += float(await fn(user_id, day))
        except Exception as exc:  # noqa: BLE001
            log.warning("budgets.source_failed", source=name, error=type(exc).__name__)
    return total


async def _month_fraction() -> float:
    s = get_settings()
    if s.llm_monthly_ceiling_usd <= 0:
        return 0.0
    from mavis.store.repo import usage as repo

    first = timeutil.now().date().replace(day=1)
    return (await repo.month_micros(first)) / 1_000_000 / s.llm_monthly_ceiling_usd


async def state_for(user_id: int) -> BudgetState:
    s = get_settings()
    if not s.budget_enforced or not user_id:
        return BudgetState.OK
    user = await users.get(user_id)
    if user.tier == "owner":
        return BudgetState.OK
    hard = user.budget_override_usd_day or s.budget_hard_usd.get(user.tier, s.budget_hard_usd["standard"])
    soft = hard / 2 if user.budget_override_usd_day else s.budget_soft_usd.get(user.tier, hard / 2)
    spent = await spend_today_usd(user_id)
    if spent >= hard * s.budget_runaway_factor:
        state = BudgetState.RUNAWAY
    elif spent >= hard:
        state = BudgetState.HARD
    elif spent >= soft:
        state = BudgetState.SOFT
    else:
        state = BudgetState.OK
    if state is BudgetState.OK and user.tier == "standard" and await _month_fraction() >= 0.9:
        state = BudgetState.SOFT
    return state


async def notify_once(user_id: int, state: BudgetState) -> None:
    if state < BudgetState.HARD:
        return
    tz = (await users.get(user_id)).timezone
    day = timeutil.to_local(timeutil.now(), tz).date()
    text = RUNAWAY_TEXT if state is BudgetState.RUNAWAY else HARD_CAP_TEXT
    await outbox.enqueue_now(Outbound(user_id=user_id, text=text,
                                      dedupe_key=f"budget:{user_id}:{day}:{state.name}"))


async def start_cooldown(user_id: int, minutes: int = 60) -> None:
    client = bus.get_redis()
    if client is not None:
        await client.set(f"mavis:cooldown:u{user_id}:x", "1", ex=minutes * 60)
    else:
        _cool_mem[user_id] = timeutil.now().timestamp() + minutes * 60


async def in_cooldown(user_id: int) -> bool:
    client = bus.get_redis()
    if client is not None:
        return bool(await client.exists(f"mavis:cooldown:u{user_id}:x"))
    return _cool_mem.get(user_id, 0) > timeutil.now().timestamp()


async def note_rate_limit_hit(user_id: int) -> None:
    now = timeutil.now().timestamp()
    client = bus.get_redis()
    if client is not None:
        key = f"mavis:rlhits:u{user_id}:x"
        await client.zadd(key, {str(now): now})
        await client.zremrangebyscore(key, 0, now - 3600)
        await client.expire(key, 3700)
        hits = int(await client.zcard(key))
    else:
        _hits_mem[user_id] = [t for t in _hits_mem.get(user_id, []) if t > now - 3600] + [now]
        hits = len(_hits_mem[user_id])
    if hits >= 5 and not await in_cooldown(user_id):
        await start_cooldown(user_id)
        from mavis.obs.watchdog import alert_owner  # Task 16; until then a log line

        await alert_owner(f"user #{user_id} is in a 1 h cooldown after {hits} rate-limit hits",
                          key=f"cool:{user_id}")


async def ban(user_id: int, reason: str, *, purge: bool = False) -> None:
    await users.update(user_id, status="banned", banned_at=utcnow(), ban_reason=reason[:200] or None)
    async with Session() as s:
        await s.execute(update(WakeupRow).where(WakeupRow.user_id == user_id, WakeupRow.status == "pending")
                        .values(status="cancelled"))
        await s.execute(delete(OutboxMessage).where(OutboxMessage.user_id == user_id,
                                                    OutboxMessage.status.in_(("pending", "sending"))))
        await s.commit()
    await audit.record(user_id, actor="owner", action="user.banned", detail={"purge": purge})
    if purge:
        from mavis.access.deletion import request_deletion  # Task 14

        await request_deletion(user_id, by_owner=True)


async def unban(user_id: int) -> None:
    await users.update(user_id, status="active", banned_at=None, ban_reason=None)
    await audit.record(user_id, actor="owner", action="user.unbanned", detail={})


async def _ban_cmd(event, owner, args):
    if not args or not args[0].isdigit():
        return "Use: /ban <user id> [purge] [reason]"
    purge = len(args) > 1 and args[1].lower() == "purge"
    reason = " ".join(args[2 if purge else 1:])
    await ban(int(args[0]), reason, purge=purge)
    return f"User #{args[0]} is paused{' and their data is being deleted' if purge else ''}."


async def _unban_cmd(event, owner, args):
    if not args or not args[0].isdigit():
        return "Use: /unban <user id>"
    await unban(int(args[0]))
    return f"User #{args[0]} is active again."


def register() -> None:
    from mavis.access.commands import register_owner_command

    register_owner_command("ban", _ban_cmd)
    register_owner_command("unban", _unban_cmd)
