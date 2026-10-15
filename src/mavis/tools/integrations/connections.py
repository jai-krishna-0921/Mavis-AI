# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Per-user connection status, cached briefly so every tool call doesn't hit the provider."""

from __future__ import annotations

import time
from collections.abc import Callable

from mavis.domain.errors import ConnectionRequired
from mavis.domain.integrations import ConnectionState, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.base import IntegrationProvider

_AUTH_MARKERS = ("answered 401", "answered 403", "unauthor", "invalid_grant", "token expired",
                 "expired token", "revoked", "reauthor")


def is_auth_error(error: str | None) -> bool:
    """Does a provider error text look like the account's token no longer works?"""
    text = (error or "").lower()
    return any(m in text for m in _AUTH_MARKERS)


def failed_alias(
    states: dict[str, ConnectionState], capabilities: tuple[Capability, ...]
) -> Capability | None:
    """The first of the capabilities whose account existed and stopped working, if any."""
    return next((c for c in capabilities if states.get(c.value) is ConnectionState.FAILED), None)


class ConnectionCache:
    def __init__(
        self, provider: IntegrationProvider, ttl_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._provider = provider
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[int, tuple[float, dict[str, ConnectionState]]] = {}
        self._generation: dict[int, int] = {}

    async def status(self, user_id: int, *, fresh: bool = False) -> dict[str, ConnectionState]:
        now = self._clock()
        hit = self._entries.get(user_id)
        if hit and not fresh and now < hit[0]:
            return dict(hit[1])
        generation = self._generation.get(user_id, 0)
        states = await self._provider.status(UserRef(user_id=user_id))
        if self._generation.get(user_id, 0) == generation:
            # Only cache if nobody invalidated while the fetch was in flight.
            self._entries[user_id] = (now + self._ttl, dict(states))
        return dict(states)

    def invalidate(self, user_id: int) -> None:
        self._generation[user_id] = self._generation.get(user_id, 0) + 1
        self._entries.pop(user_id, None)

    async def is_active(self, user_id: int, capability: Capability, *, fresh: bool = False) -> bool:
        return (await self.status(user_id, fresh=fresh)).get(capability.value) is ConnectionState.ACTIVE

    async def ensure(
        self, user_id: int, capability: Capability, reason: str, *, revoked: bool = False
    ) -> None:
        state = (await self.status(user_id)).get(capability.value)
        if state is not ConnectionState.ACTIVE:
            # FAILED means the account existed and stopped working: ask to reconnect, not to connect.
            raise ConnectionRequired(capability, reason, revoked=revoked or state is ConnectionState.FAILED)

    async def is_active_any(self, user_id: int, capabilities: tuple[Capability, ...], *,
                            fresh: bool = False) -> bool:
        states = await self.status(user_id, fresh=fresh)
        return any(states.get(c.value) is ConnectionState.ACTIVE for c in capabilities)

    async def ensure_any(self, user_id: int, capabilities: tuple[Capability, ...], reason: str) -> None:
        """Pass when any of the capabilities is ACTIVE. Otherwise ask for the FAILED one (reconnect it, not
        a different service), else for the first."""
        states = await self.status(user_id)
        if any(states.get(c.value) is ConnectionState.ACTIVE for c in capabilities):
            return
        failed = failed_alias(states, capabilities)
        raise ConnectionRequired(failed or capabilities[0], reason, revoked=failed is not None)
