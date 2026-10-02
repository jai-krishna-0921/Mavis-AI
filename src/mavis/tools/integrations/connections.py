"""Per-user connection status, cached briefly so every tool call doesn't hit the provider."""

from __future__ import annotations

import time
from collections.abc import Callable

from mavis.domain.errors import ConnectionRequired
from mavis.domain.integrations import ConnectionState, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.base import IntegrationProvider


class ConnectionCache:
    def __init__(
        self, provider: IntegrationProvider, ttl_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._provider = provider
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[int, tuple[float, dict[str, ConnectionState]]] = {}

    async def status(self, user_id: int, *, fresh: bool = False) -> dict[str, ConnectionState]:
        now = self._clock()
        hit = self._entries.get(user_id)
        if hit and not fresh and now < hit[0]:
            return hit[1]
        states = await self._provider.status(UserRef(user_id=user_id))
        self._entries[user_id] = (now + self._ttl, states)
        return states

    def invalidate(self, user_id: int) -> None:
        self._entries.pop(user_id, None)

    async def is_active(self, user_id: int, capability: Capability, *, fresh: bool = False) -> bool:
        return (await self.status(user_id, fresh=fresh)).get(capability.value) is ConnectionState.ACTIVE

    async def ensure(
        self, user_id: int, capability: Capability, reason: str, *, revoked: bool = False
    ) -> None:
        if not await self.is_active(user_id, capability):
            raise ConnectionRequired(capability, reason, revoked=revoked)
