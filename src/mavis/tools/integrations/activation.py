"""On a new ACTIVE connection: subscribe push triggers; if that's impossible, start polling."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import structlog

from mavis.domain import timeutil
from mavis.domain.errors import IntegrationError
from mavis.domain.integrations import UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.composio_map import MAVIS_TRIGGERS
from mavis.tools.integrations.connect_flow import Schedule, UserState
from mavis.tools.integrations.poller import POLL_KIND, POLLABLE

log = structlog.get_logger()


class Activator:
    def __init__(
        self, *, provider: IntegrationProvider, state: UserState, schedule: Schedule, polling_forced: bool,
        clock: Callable[[], datetime] = timeutil.now,
    ) -> None:
        self.provider, self.state, self.schedule = provider, state, schedule
        self.polling_forced, self.clock = polling_forced, clock

    async def on_active(self, user_id: int, capability: Capability) -> bool:
        subscribed = not self.polling_forced
        if subscribed:
            for trigger in MAVIS_TRIGGERS.get(capability, ()):
                try:
                    await self.provider.subscribe(UserRef(user_id=user_id), trigger, {})
                except IntegrationError as exc:
                    log.warning("activation.subscribe_failed", trigger=trigger, error=str(exc))
                    subscribed = False
        poll = not subscribed and capability in POLLABLE
        polling = dict((await self.state.get(user_id)).get("polling", {}))
        polling[capability.value] = poll
        await self.state.update(user_id, {"polling": polling})
        if poll:
            await self.schedule(user_id, self.clock(), capability.value, POLL_KIND)
        return poll
