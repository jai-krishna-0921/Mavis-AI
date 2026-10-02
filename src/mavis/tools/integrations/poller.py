"""Polling fallback that emits exactly the events webhooks would. Self-rescheduling; no cron."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog

from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.integrations import ConnectionState, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connect_flow import Schedule, UserState
from mavis.tools.integrations.connections import ConnectionCache, is_auth_error
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


class _AuthError(Exception):
    """The provider rejected the call as unauthorised; the connection may have been revoked."""


OnFailed = Callable[[int, Capability], Awaitable[object]]
UserIds = Callable[[], Awaitable[list[int]]]


class Poller:
    def __init__(
        self, *, provider: IntegrationProvider, cache: ConnectionCache, bus: EventBus, state: UserState,
        schedule: Schedule, on_failed: OnFailed | None = None, user_ids: UserIds | None = None,
        clock: Callable[[], datetime] = timeutil.now,
    ) -> None:
        self.provider, self.cache, self.bus, self.state, self.schedule, self.clock = (
            provider, cache, bus, state, schedule, clock,
        )
        self.on_failed, self.user_ids = on_failed, user_ids

    async def on_wakeup(self, user_id: int, reason: str) -> None:
        try:
            capability = Capability(reason)
        except ValueError:
            return
        await self.poll(user_id, capability)

    async def _reschedule(self, user_id: int, capability: Capability) -> None:
        await self.schedule(user_id, self.clock() + POLL_INTERVAL, capability.value, POLL_KIND)

    async def ensure_chains(self, user_id: int) -> int:
        """Re-arm a missing poll wakeup for every capability this user polls. The scheduler collapses
        onto an existing pending one, so this is safe to call at any time. Returns how many were asked."""
        polling = (await self.state.get(user_id)).get("polling", {})
        armed = 0
        for capability in POLLABLE:
            if polling.get(capability.value):
                await self.schedule(user_id, self.clock(), capability.value, POLL_KIND)
                armed += 1
        return armed

    async def ensure_all_chains(self) -> int:
        """Worker startup: every user with polling enabled gets a pending poll wakeup."""
        if self.user_ids is None:
            return 0
        armed = 0
        for user_id in await self.user_ids():
            try:
                armed += await self.ensure_chains(user_id)
            except Exception as exc:  # noqa: BLE001 - one user must not block the rest
                log.warning("poller.ensure_failed", user_id=user_id, error=type(exc).__name__)
        return armed

    async def _stop(self, user_id: int, st: dict, capability: Capability, state: ConnectionState) -> bool:
        """The provider definitely says this capability is not active. True if the chain really stopped."""
        if state is ConnectionState.INITIATED:
            return True  # mid sign-in: activation restarts the chain when it finishes
        if state is ConnectionState.FAILED and self.on_failed is not None:
            try:
                await self.on_failed(user_id, capability)  # deduped per capability per day
            except Exception as exc:  # noqa: BLE001 - the user was not told yet: keep the chain, retry
                log.warning("poller.reconnect_prompt_failed", user_id=user_id, error=type(exc).__name__)
                return False
        polling = dict(st.get("polling", {}))
        polling.pop(capability.value, None)  # a later /connect sees the gap and re-activates
        await self.state.update(user_id, {"polling": polling})
        return True

    async def poll(self, user_id: int, capability: Capability) -> int:
        try:
            st = await self.state.get(user_id)
        except Exception as exc:
            log.warning("poller.state_failed", user_id=user_id, error=type(exc).__name__)
            await self._reschedule(user_id, capability)
            return 0
        if capability not in POLLABLE or not st.get("polling", {}).get(capability.value):
            return 0
        try:
            states = None
            if not await self.cache.is_active(user_id, capability):
                # Never stop on a possibly stale cache entry: ask the provider before ending the chain.
                states = await self.cache.status(user_id, fresh=True)
        except Exception as exc:
            # Unknown, not "disconnected": keep the chain alive and try again next interval.
            log.warning("poller.status_failed", user_id=user_id, error=type(exc).__name__)
            await self._reschedule(user_id, capability)
            return 0
        if states is not None and states.get(capability.value) is not ConnectionState.ACTIVE:
            if await self._stop(user_id, st, capability, states.get(capability.value, ConnectionState.NONE)):
                return 0
            await self._reschedule(user_id, capability)
            return 0
        reschedule = True
        try:
            if capability is Capability.GMAIL:
                return await self._poll_gmail(user_id, st)
            return await self._poll_calendar(user_id, st)
        except _AuthError:
            reschedule = not await self._auth_failed(user_id, st, capability)
            return 0
        finally:
            if reschedule:
                await self._reschedule(user_id, capability)

    async def _auth_failed(self, user_id: int, st: dict, capability: Capability) -> bool:
        """An auth-looking error: only a definite FAILED from the provider ends the chain."""
        try:
            state = (await self.cache.status(user_id, fresh=True)).get(capability.value, ConnectionState.NONE)
            if state is ConnectionState.FAILED:
                return await self._stop(user_id, st, capability, state)
        except Exception as exc:  # noqa: BLE001
            log.warning("poller.status_failed", user_id=user_id, error=type(exc).__name__)
        return False

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
            if is_auth_error(res.error):
                raise _AuthError
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
            if is_auth_error(res.error):
                raise _AuthError
            return 0
        published = 0
        for raw in extract_calendar_items(res.data):
            event = calendar_event(user_id, raw, source="poller")
            if event is not None and await self.bus.publish(event):
                published += 1
        await self._set_cursor(user_id, st, "gcal_updated_min", now.isoformat())
        return published
