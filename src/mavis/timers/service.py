"""wake_me: the only way time enters the initiative engine."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any

from mavis.domain import timeutil
from mavis.domain.wakeups import Wakeup, WakeupKind
from mavis.store.repo import wakeups as repo


class WakeupService:
    async def wake_me(
        self,
        user_id: int,
        at: datetime,
        reason: str,
        loop_id: int | None = None,
        kind: WakeupKind | str = WakeupKind.AGENT,
        *,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        scale: bool = True,
    ) -> int:
        kind = WakeupKind(kind)
        at = timeutil.ensure_utc(at)
        now = timeutil.now()
        if scale and at > now:
            at = now + timeutil.scale_offset(at - now)
        if dedupe_key and (existing := await repo.pending_by_key(user_id, dedupe_key)) is not None:
            return existing.id
        return await repo.insert(user_id=user_id, due_at=at, kind=kind, reason=reason, loop_id=loop_id,
                                 payload=payload or {}, dedupe_key=dedupe_key)

    async def cancel(self, wakeup_id: int) -> bool:
        return await repo.cancel_ids([wakeup_id]) == 1

    async def cancel_where(
        self, user_id: int, kinds: Iterable[WakeupKind], loop_id: int | None = None
    ) -> int:
        return await repo.cancel_where(user_id, kinds, loop_id)

    async def reschedule(self, wakeup_id: int, at: datetime) -> bool:
        return await repo.reschedule(wakeup_id, timeutil.ensure_utc(at))

    async def pending(self, user_id: int, kind: WakeupKind | None = None) -> list[Wakeup]:
        return await repo.list_pending(user_id, kind)

    async def claim_due(self, now: datetime, limit: int = 50) -> list[Wakeup]:
        return await repo.claim_due(timeutil.ensure_utc(now), limit)

    async def fire_due(self, now: datetime, publish: Callable[[Wakeup], Awaitable[None]],
                       limit: int = 50) -> list[Wakeup]:
        """Publish each due wakeup, then mark it fired (retried on the next tick if publishing fails)."""
        return await repo.fire_due(timeutil.ensure_utc(now), limit, publish)
