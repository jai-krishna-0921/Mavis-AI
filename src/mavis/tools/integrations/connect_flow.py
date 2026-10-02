"""Ask for a connection exactly when it's needed, then pick the interrupted work back up.

Flow: interrupt(connect) -> start(): link + 'Not now' -> checks at +1/+3/+10 min and on callback ->
CONNECTION_CHANGED -> resume waiting runs, first sync, trigger/poller activation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.errors import IntegrationError
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.domain.integrations import ConnectionState, PendingStatus, UserRef
from mavis.domain.messages import Button, Outbound
from mavis.domain.policy import Capability
from mavis.store.repo import connections
from mavis.tools.integrations.actions import (
    BRANDS,
    CAPABILITY_PURPOSE,
    DISPLAY_NAMES,
    INTEGRATION_CAPABILITIES,
)
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache

log = structlog.get_logger()

CHECK_KIND = "system_connection_check"
CHECK_DELAYS = (timedelta(minutes=1), timedelta(minutes=3), timedelta(minutes=10))
PENDING_TTL = timedelta(hours=24)
REUSE_WINDOW = timedelta(minutes=10)
NOT_NOW_PREFIX = "conn:no:"
START_PREFIX = "conn:start:"
RETRY_PREFIX = "conn:retry:"

Notify = Callable[[Outbound], Awaitable[None]]
Schedule = Callable[[int, datetime, str, str], Awaitable[int]]  # (user_id, at, reason, kind)
OnActive = Callable[[int, Capability], Awaitable[None]]


class UserState(Protocol):
    async def get(self, user_id: int) -> dict: ...
    async def update(self, user_id: int, patch: dict) -> dict: ...


class RepoUserState:
    async def get(self, user_id: int) -> dict:
        from mavis.store.repo import users

        return await users.get_state(user_id)

    async def update(self, user_id: int, patch: dict) -> dict:
        from mavis.store.repo import users

        return await users.update_state(user_id, patch)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class ConnectFlow:
    def __init__(
        self,
        *,
        provider: IntegrationProvider,
        cache: ConnectionCache,
        bus: EventBus,
        notify: Notify,
        schedule: Schedule,
        state: UserState,
        base_url: str,
        on_active: OnActive | None = None,
        clock: Callable[[], datetime] = timeutil.now,
    ) -> None:
        self.provider, self.cache, self.bus = provider, cache, bus
        self.notify, self.schedule, self.state = notify, schedule, state
        self.base_url = base_url.rstrip("/")
        self.on_active = on_active
        self.clock = clock

    async def send(self, user_id: int, text: str, buttons: list[list[Button]] | None = None) -> None:
        await self.notify(Outbound(user_id=user_id, text=text, buttons=buttons or []))

    async def _resume(self, task_id: str, user_id: int, connected: bool, tag: str) -> None:
        await self.bus.enqueue(Job(
            id=f"resume:{task_id}:{tag}", user_id=user_id, kind=JobKind.RESUME_TASK,
            payload={"task_id": task_id, "value": {"connected": connected}},
        ))

    # --- starting a connection ---------------------------------------------------------------------

    async def start(
        self, user_id: int, capability: Capability, reason: str = "", task_id: str | None = None,
        revoked: bool = False,
    ) -> int | None:
        name = DISPLAY_NAMES[capability]
        if not revoked and await self.cache.is_active(user_id, capability, fresh=True):
            if task_id:
                await self._resume(task_id, user_id, True, f"already:{capability.value}")
            else:
                await self.send(user_id, f"{name} is already connected ✓")
            return None

        now = self.clock()
        recent = await connections.latest_open(user_id, capability)
        if recent is not None and now - _aware(recent.created_at) < REUSE_WINDOW:
            # A link is already out; just remember this run so it resumes too.
            return await connections.create_pending(user_id, capability, reason, task_id, now=now)

        pending_id = await connections.create_pending(user_id, capability, reason, task_id, now=now)
        callback = f"{self.base_url}/connect/callback?p={pending_id}"
        try:
            url = await self.provider.connect_link(UserRef(user_id=user_id), capability.value, callback)
        except IntegrationError as exc:
            log.warning("connect.link_failed", capability=capability.value, error=str(exc))
            await connections.resolve(pending_id, PendingStatus.FAILED, now=now)
            await self.send(user_id, f"I'd need {name} for that, but I can't open a connection right now. "
                                     "Mind if we try again in a bit?")
            if task_id:
                await self._resume(task_id, user_id, False, f"linkfail:{pending_id}")
            return None

        if revoked:
            lead = f"Your {name} access has expired. One tap to reconnect:"
        elif reason:
            lead = f"To {reason}, I need access to your {name}. One tap here:"
        else:
            lead = f"Let's connect your {name}. One tap here:"
        text = (f"{lead}\nYou'll sign in on {BRANDS[capability]}'s own page. No password comes to me, "
                "and you can revoke access anytime.")
        await self.send(user_id, text, [
            [Button(label=f"Connect {name}", url=url)],
            [Button(label="Not now", data=f"{NOT_NOW_PREFIX}{pending_id}")],
        ])
        for delay in CHECK_DELAYS:
            await self.schedule(user_id, now + delay, str(pending_id), CHECK_KIND)
        return pending_id

    async def on_connect_interrupt(self, task_id: int | str, user_id: int, payload: dict[str, Any]) -> None:
        """Phase 4 interrupt handler for {"type": "connect"} (dormant in this slice; Phase 4 registers it)."""
        capability = Capability(payload["capability"])
        await self.start(
            user_id, capability, CAPABILITY_PURPOSE.get(capability, ""), task_id=str(task_id),
            revoked=bool(payload.get("revoked")),
        )

    # --- learning the outcome --------------------------------------------------------------------

    async def check(self, pending_id: int) -> None:
        p = await connections.get_pending(pending_id)
        if p is None or p.status not in (PendingStatus.PENDING.value, PendingStatus.DECLINED.value):
            return
        declined = p.status == PendingStatus.DECLINED.value
        capability = Capability(p.capability)
        if not declined and self.clock() - _aware(p.created_at) > PENDING_TTL:
            await connections.resolve(pending_id, PendingStatus.EXPIRED, now=self.clock())
            return
        state = (await self.cache.status(p.user_id, fresh=True)).get(capability.value, ConnectionState.NONE)
        # After "Not now" the user may still have finished the sign-in, so ACTIVE still counts.
        accepted = (ConnectionState.ACTIVE,) if declined else (ConnectionState.ACTIVE, ConnectionState.FAILED)
        if state not in accepted:
            return
        await self.bus.publish(Event(
            id=f"conn:{p.user_id}:{capability.value}:{state.value.lower()}:{pending_id}",
            user_id=p.user_id, type=EventType.CONNECTION_CHANGED, occurred_at=self.clock(),
            source="integrations", trust=Trust.SYSTEM,
            payload={"capability": capability.value, "state": state.value, "pending_id": pending_id},
        ))

    async def on_check_wakeup(self, user_id: int, reason: str) -> None:
        try:
            pending_id = int(reason)
        except ValueError:
            return
        await self.check(pending_id)

    async def on_connection_changed(self, event: Event) -> None:
        capability = Capability(event.payload["capability"])
        state = ConnectionState(event.payload["state"])
        user_id = event.user_id
        name = DISPLAY_NAMES[capability]
        self.cache.invalidate(user_id)
        waiting = await connections.open_for(user_id, capability)

        if state is ConnectionState.FAILED:
            for p in waiting:
                await connections.resolve(p.id, PendingStatus.FAILED, now=self.clock())
                if p.task_id:
                    await self._resume(p.task_id, user_id, False, f"failed:{p.id}")
            if waiting:
                await self.send(user_id, f"Hmm, the {name} connection didn't go through. Want a fresh link?",
                                [[Button(label="Try again", data=f"{RETRY_PREFIX}{capability.value}")]])
            return
        if state is not ConnectionState.ACTIVE:
            return

        resumed = False
        for p in waiting:
            await connections.resolve(p.id, PendingStatus.ACTIVE, now=self.clock())
            if p.task_id:
                await self._resume(p.task_id, user_id, True, f"conn:{p.id}")
                resumed = True

        synced = dict((await self.state.get(user_id)).get("synced", {}))
        first_time = not synced.get(capability.value)
        if first_time:
            await self.bus.enqueue(Job(id=f"first_sync:{user_id}:{capability.value}", user_id=user_id,
                                       kind=JobKind.FIRST_SYNC, payload={"capability": capability.value}))
            synced[capability.value] = self.clock().isoformat()
            await self.state.update(user_id, {"synced": synced})

        if self.on_active is not None:
            await self.on_active(user_id, capability)
        # One announcement per activation: later duplicate events (other pendings) stay quiet.
        if not resumed and (waiting or first_time):
            await self.send(user_id, f"Connected ✓ I can see your {name} now. "
                                     "Give me a minute to get familiar with it.")

    # --- user controls ----------------------------------------------------------------------------

    async def decline(self, user_id: int, pending_id: int) -> None:
        p = await connections.get_pending(pending_id)
        if p is None or p.user_id != user_id or p.status != PendingStatus.PENDING.value:
            return
        await connections.resolve(pending_id, PendingStatus.DECLINED, now=self.clock())
        not_now = dict((await self.state.get(user_id)).get("not_now", {}))
        not_now[p.capability] = self.clock().isoformat()
        await self.state.update(user_id, {"not_now": not_now})
        if p.task_id:
            await self._resume(p.task_id, user_id, False, f"declined:{pending_id}")
        else:
            await self.send(user_id, "No problem. Just say the word whenever.")

    async def on_button(self, event: Event, data: str) -> None:
        try:
            if data.startswith(NOT_NOW_PREFIX):
                await self.decline(event.user_id, int(data.removeprefix(NOT_NOW_PREFIX)))
            elif data.startswith((START_PREFIX, RETRY_PREFIX)):
                capability = Capability(data.rsplit(":", 1)[1])
                if capability in INTEGRATION_CAPABILITIES:
                    await self.start(event.user_id, capability, "")
        except ValueError:
            log.warning("connect.bad_button", data=data)

    async def offer_menu(self, user_id: int) -> None:
        states = await self.cache.status(user_id, fresh=True)
        rows = [
            [Button(label=f"Connect {DISPLAY_NAMES[c]}", data=f"{START_PREFIX}{c.value}")]
            for c in INTEGRATION_CAPABILITIES if states.get(c.value) is not ConnectionState.ACTIVE
        ]
        if not rows:
            await self.send(user_id, "Everything's already connected: Gmail, Calendar, Slack and Notion.")
            return
        await self.send(user_id, "Which one should I hook up?", rows)

    async def status_text(self, user_id: int) -> str:
        states = await self.cache.status(user_id, fresh=True)
        lines = []
        for c in INTEGRATION_CAPABILITIES:
            active = states.get(c.value) is ConnectionState.ACTIVE
            mark, word = ("✅", "connected") if active else ("⚪", "not connected")
            lines.append(f"{mark} {DISPLAY_NAMES[c]}: {word}")
        return "\n".join(lines)

    async def disconnect(self, user_id: int, capability: Capability) -> None:
        name = DISPLAY_NAMES[capability]
        try:
            await self.provider.disconnect(UserRef(user_id=user_id), capability.value)
        except IntegrationError as exc:
            log.warning("connect.disconnect_failed", capability=capability.value, error=str(exc))
            await self.send(user_id, f"I couldn't disconnect {name} just now. Mind trying again in a bit?")
            return
        self.cache.invalidate(user_id)
        synced = dict((await self.state.get(user_id)).get("synced", {}))
        if synced.pop(capability.value, None) is not None:
            await self.state.update(user_id, {"synced": synced})
        await self.send(user_id, f"Disconnected {name}. I can't see it anymore.")
