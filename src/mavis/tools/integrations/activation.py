"""On a new ACTIVE connection: subscribe push triggers; if that's impossible, start polling."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import structlog

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.errors import IntegrationError
from mavis.domain.integrations import UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import workspace_enabled
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.composio_map import TRIGGER_CONFIGS, triggers_for
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
        # per googlesuper fan-out in progress: users, and the subset whose triggers did not all attach
        self._fanout: set[int] = set()
        self._google_subscribe_failed: set[int] = set()

    async def _native(self, user_id: int, capability: Capability) -> bool:
        """An ACTIVE native grant serves this capability: there are no push triggers for it, polling is the
        inbound path (Slack's poll is the safety net behind the Events API)."""
        check = getattr(self.provider, "uses_native", None)
        if check is None or capability not in POLLABLE:
            return False
        try:
            return bool(await check(user_id, capability))
        except Exception as exc:  # noqa: BLE001 - unknown: fall back to the trigger/poll rules below
            log.warning("activation.native_check_failed", error=type(exc).__name__)
            return False

    async def on_active(self, user_id: int, capability: Capability) -> bool:
        subscribed = not self.polling_forced and not await self._native(user_id, capability)
        if subscribed:
            # Workspace triggers (share, comment, task) only feed the attention layer, so they need it on.
            workspace = workspace_enabled() and get_settings().attention_enabled
            for trigger in triggers_for(capability, workspace=workspace):
                try:
                    await self.provider.subscribe(
                        UserRef(user_id=user_id), trigger, dict(TRIGGER_CONFIGS.get(trigger, {}))
                    )
                except IntegrationError as exc:
                    log.warning("activation.subscribe_failed", trigger=trigger, error=str(exc))
                    subscribed = False
                    if user_id in self._fanout:
                        self._google_subscribe_failed.add(user_id)
        poll = not subscribed and capability in POLLABLE
        polling = dict((await self.state.get(user_id)).get("polling", {}))
        polling[capability.value] = poll
        await self.state.update(user_id, {"polling": polling})
        if poll:
            await self.schedule(user_id, self.clock(), capability.value, POLL_KIND)
        return poll

    def begin_google(self, user_id: int) -> None:
        """A googlesuper fan-out starts: failures count from here, not from earlier activations."""
        self._fanout.add(user_id)
        self._google_subscribe_failed.discard(user_id)

    async def retire_legacy(self, user_id: int) -> int:
        """Google upgrade done: the googlesuper triggers are attached, so drop the old Gmail/Calendar ones
        (a provider without the method has nothing to retire). If any googlesuper trigger failed to
        attach, the old ones stay so the user keeps getting events."""
        failed = user_id in self._google_subscribe_failed
        self._google_subscribe_failed.discard(user_id)
        self._fanout.discard(user_id)
        retire = getattr(self.provider, "retire_legacy_triggers", None)
        if retire is None or self.polling_forced or failed:
            return 0
        try:
            return int(await retire(UserRef(user_id=user_id)))
        except IntegrationError as exc:
            log.warning("activation.retire_failed", error=str(exc))
            return 0
