"""Owner alerts (spec 11). Task 16 grows this module (watchdog loop, dead-man ping)."""

from __future__ import annotations

import structlog

from mavis.domain.messages import Outbound
from mavis.store.db import utcnow
from mavis.store.repo import outbox, users

log = structlog.get_logger(__name__)


async def alert_owner(text: str, key: str) -> None:
    """Tell every owner, at most once per hour for the same `key`."""
    log.warning("watchdog.alert", key=key)
    hour = utcnow().strftime("%Y%m%d%H")
    for owner in await users.owners():
        await outbox.enqueue_now(Outbound(user_id=owner.id, text=text,
                                          dedupe_key=f"alert:{key}:{hour}:{owner.id}"))
