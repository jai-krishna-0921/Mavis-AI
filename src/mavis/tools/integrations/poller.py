"""Polling fallback that emits exactly the events webhooks would. Self-rescheduling; no cron."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import structlog

from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.integrations import UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connect_flow import Schedule, UserState
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.normalize import (
    calendar_event,
    email_event,
    extract_calendar_items,
    extract_messages,
    normalize_email,
)

log = structlog.get_logger()

POLL_KIND = "system_poll"
POLL_INTERVAL = timedelta(minutes=2)
POLLABLE = frozenset({Capability.GMAIL, Capability.CALENDAR})
INITIAL_LOOKBACK = timedelta(minutes=10)


class Poller:
    def __init__(
        self, *, provider: IntegrationProvider, cache: ConnectionCache, bus: EventBus, state: UserState,
        schedule: Schedule, clock: Callable[[], datetime] = timeutil.now,
    ) -> None:
        self.provider, self.cache, self.bus, self.state, self.schedule, self.clock = (
            provider, cache, bus, state, schedule, clock,
        )

    async def on_wakeup(self, user_id: int, reason: str) -> None:
        try:
            capability = Capability(reason)
        except ValueError:
            return
        await self.poll(user_id, capability)

    async def poll(self, user_id: int, capability: Capability) -> int:
        st = await self.state.get(user_id)
        if capability not in POLLABLE or not st.get("polling", {}).get(capability.value):
            return 0
        try:
            if not await self.cache.is_active(user_id, capability):
                return 0  # definite answer: stop the chain; activation restarts it on reconnect
        except Exception as exc:
            # Unknown, not "disconnected": keep the chain alive and try again next interval.
            log.warning("poller.status_failed", user_id=user_id, error=type(exc).__name__)
            await self.schedule(user_id, self.clock() + POLL_INTERVAL, capability.value, POLL_KIND)
            return 0
        try:
            if capability is Capability.GMAIL:
                return await self._poll_gmail(user_id, st)
            return await self._poll_calendar(user_id, st)
        finally:
            await self.schedule(user_id, self.clock() + POLL_INTERVAL, capability.value, POLL_KIND)

    async def _set_cursor(self, user_id: int, st: dict, key: str, value: object) -> None:
        cursors = dict(st.get("cursors", {}))
        cursors[key] = value
        await self.state.update(user_id, {"cursors": cursors})

    async def _poll_gmail(self, user_id: int, st: dict) -> int:
        after = int(st.get("cursors", {}).get("gmail_after") or (self.clock() - INITIAL_LOOKBACK).timestamp())
        res = await self.provider.execute(
            UserRef(user_id=user_id), "mail.search", {"query": f"after:{after} -in:sent", "max_results": 25}
        )
        if not res.ok:
            log.warning("poller.gmail_failed", user_id=user_id, error=res.error)
            return 0
        ceiling = int(self.clock().timestamp())
        newest, published = after, 0
        for raw in extract_messages(res.data):
            event = email_event(user_id, raw, source="poller")
            if event is None:
                continue
            if normalize_email(raw)["received_at"]:  # no timestamp: leave the cursor alone
                newest = max(newest, min(int(event.occurred_at.timestamp()), ceiling))
            if await self.bus.publish(event):
                published += 1
        await self._set_cursor(user_id, st, "gmail_after", newest)
        return published

    async def _poll_calendar(self, user_id: int, st: dict) -> int:
        now = self.clock()
        updated_min = st.get("cursors", {}).get("gcal_updated_min") or (now - INITIAL_LOOKBACK).isoformat()
        res = await self.provider.execute(UserRef(user_id=user_id), "calendar.list", {
            "time_min": now.isoformat(), "time_max": (now + timedelta(days=30)).isoformat(),
            "max_results": 50, "updated_min": updated_min,
        })
        if not res.ok:
            log.warning("poller.calendar_failed", user_id=user_id, error=res.error)
            return 0
        published = 0
        for raw in extract_calendar_items(res.data):
            event = calendar_event(user_id, raw, source="poller")
            if event is not None and await self.bus.publish(event):
                published += 1
        await self._set_cursor(user_id, st, "gcal_updated_min", now.isoformat())
        return published
