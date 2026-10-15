# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Morning-brief context from connected services. Returns None when a service isn't connected."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from mavis.domain.integrations import ConnectionState, UserRef
from mavis.domain.policy import Capability
from mavis.initiative.email_triage import AUTOMATED, classify
from mavis.initiative.routines import BriefItem
from mavis.tools.integrations.actions import serving
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache, is_auth_error
from mavis.tools.integrations.normalize import (
    extract_calendar_items,
    extract_messages,
    normalize_calendar_event,
    normalize_email,
    to_datetime,
)

log = structlog.get_logger()

OnFailed = Callable[[int, Capability], Awaitable[object]]

CALENDAR_EMPTY = "Calendar today: nothing scheduled."
INBOX_EMPTY = "Inbox: nothing unread that needs you."


async def usable(
    cache: ConnectionCache, user_id: int, capability: Capability, on_failed: OnFailed | None,
    *, fresh: bool = False,
) -> bool:
    """Is any account serving this capability active (Gmail or Outlook for mail)? A FAILED one (expired or
    revoked token) triggers the reconnect prompt for that account."""
    states = await cache.status(user_id, fresh=fresh)
    aliases = serving(capability)
    for alias in aliases:
        if states.get(alias.value) is ConnectionState.FAILED:
            await _prompt(on_failed, user_id, alias)
    return any(states.get(a.value) is ConnectionState.ACTIVE for a in aliases)


async def _prompt(on_failed: OnFailed | None, user_id: int, capability: Capability) -> None:
    if on_failed is None:
        return
    try:
        await on_failed(user_id, capability)
    except Exception as exc:  # noqa: BLE001 - the brief itself must still go out
        log.warning("brief.reconnect_prompt_failed", user_id=user_id, error=type(exc).__name__)


async def auth_failed(
    cache: ConnectionCache, user_id: int, capability: Capability, error: str | None,
    on_failed: OnFailed | None,
) -> None:
    """A source call failed with an auth-looking error: confirm with the provider, then prompt."""
    if not is_auth_error(error):
        return
    try:
        await usable(cache, user_id, capability, on_failed, fresh=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("brief.status_failed", user_id=user_id, error=type(exc).__name__)


def _account(raw: dict) -> str:
    """With several accounts connected each item says which one it came from."""
    return f" [{raw['account']}]" if raw.get("account") else ""


def _notes(data: object) -> list[str]:
    """Which account could not be reached (a merged read answers with the others)."""
    notes = data.get("notes") if isinstance(data, dict) else None
    return [n for n in notes if isinstance(n, str)] if isinstance(notes, list) else []


class CalendarBrief:
    name = "calendar"

    def __init__(
        self, provider: IntegrationProvider, cache: ConnectionCache, tz_of: Callable[[int], Awaitable[str]],
        on_failed: OnFailed | None = None,
    ) -> None:
        self.provider, self.cache, self.tz_of, self.on_failed = provider, cache, tz_of, on_failed

    async def gather(self, user_id: int, now: datetime) -> str | None:
        if not await usable(self.cache, user_id, Capability.CALENDAR, self.on_failed):
            return None
        tz = ZoneInfo(await self.tz_of(user_id))
        start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        res = await self.provider.execute(
            UserRef(user_id=user_id),
            "calendar.list",
            {
                "time_min": start.isoformat(),
                "time_max": (start + timedelta(days=1)).isoformat(),
                "max_results": 20,
            },
        )
        if not res.ok:
            await auth_failed(self.cache, user_id, Capability.CALENDAR, res.error, self.on_failed)
            return None
        raw = extract_calendar_items(res.data)
        notes = _notes(res.data)
        if not raw:
            return "\n".join([CALENDAR_EMPTY, *notes])
        lines = []
        for r in raw:
            e = normalize_calendar_event(r)
            begins = to_datetime(e["start"])
            when = f"{begins.astimezone(tz):%H:%M}" if begins else "all day"
            who = f" (with {', '.join(e['attendees'])})" if e["attendees"] else ""
            lines.append(f"- {when} {e['summary']}{who}{_account(r)}")
        return "Calendar today:\n" + "\n".join([*lines, *notes])

    async def items(self, user_id: int, start: datetime, end: datetime) -> list[BriefItem]:
        """Phase 3 BriefSource protocol (routines.register_brief_source)."""
        text = await self.gather(user_id, start)
        if not text:
            return []
        return [BriefItem(text, text == CALENDAR_EMPTY)]  # provider content is untrusted


class InboxBrief:
    name = "inbox"

    def __init__(
        self, provider: IntegrationProvider, cache: ConnectionCache, on_failed: OnFailed | None = None
    ) -> None:
        self.provider, self.cache, self.on_failed = provider, cache, on_failed

    async def gather(self, user_id: int, now: datetime) -> str | None:
        if not await usable(self.cache, user_id, Capability.GMAIL, self.on_failed):
            return None
        res = await self.provider.execute(
            UserRef(user_id=user_id),
            "mail.search",
            {
                "query": "is:unread in:inbox -category:promotions -category:social newer_than:2d",
                "max_results": 15,
            },
        )
        if not res.ok:
            await auth_failed(self.cache, user_id, Capability.GMAIL, res.error, self.on_failed)
            return None
        worth = []
        notes = _notes(res.data)
        for x in extract_messages(res.data):
            m = normalize_email(x)
            security = "security" in classify(m)
            noise = m["list_unsubscribe"] or any(w in m["from_address"] for w in AUTOMATED)
            if security or not noise:
                worth.append((m, x))
        if not worth:
            return "\n".join([INBOX_EMPTY, *notes])
        lines = [f"- {m['from_name']}: {m['subject']}{_account(x)}" for m, x in worth[:5]]
        return "\n".join([f"Inbox: {len(worth)} unread worth a look", *lines, *notes])

    async def items(self, user_id: int, start: datetime, end: datetime) -> list[BriefItem]:
        """Phase 3 BriefSource protocol (routines.register_brief_source)."""
        text = await self.gather(user_id, start)
        if not text:
            return []
        return [BriefItem(text, text == INBOX_EMPTY)]  # provider content is untrusted
