"""Morning-brief context from connected services. Returns None when a service isn't connected."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mavis.domain.integrations import UserRef
from mavis.domain.policy import Capability
from mavis.initiative.email_triage import AUTOMATED, classify
from mavis.initiative.routines import BriefItem
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.normalize import (
    extract_calendar_items,
    extract_messages,
    normalize_calendar_event,
    normalize_email,
    to_datetime,
)

CALENDAR_EMPTY = "Calendar today: nothing scheduled."
INBOX_EMPTY = "Inbox: nothing unread that needs you."


class CalendarBrief:
    name = "calendar"

    def __init__(
        self, provider: IntegrationProvider, cache: ConnectionCache, tz_of: Callable[[int], Awaitable[str]]
    ) -> None:
        self.provider, self.cache, self.tz_of = provider, cache, tz_of

    async def gather(self, user_id: int, now: datetime) -> str | None:
        if not await self.cache.is_active(user_id, Capability.CALENDAR):
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
            return None
        events = [normalize_calendar_event(e) for e in extract_calendar_items(res.data)]
        if not events:
            return CALENDAR_EMPTY
        lines = []
        for e in events:
            begins = to_datetime(e["start"])
            when = f"{begins.astimezone(tz):%H:%M}" if begins else "all day"
            who = f" (with {', '.join(e['attendees'])})" if e["attendees"] else ""
            lines.append(f"- {when} {e['summary']}{who}")
        return "Calendar today:\n" + "\n".join(lines)

    async def items(self, user_id: int, start: datetime, end: datetime) -> list[BriefItem]:
        """Phase 3 BriefSource protocol (routines.register_brief_source)."""
        text = await self.gather(user_id, start)
        if not text:
            return []
        return [BriefItem(text, text == CALENDAR_EMPTY)]  # provider content is untrusted


class InboxBrief:
    name = "inbox"

    def __init__(self, provider: IntegrationProvider, cache: ConnectionCache) -> None:
        self.provider, self.cache = provider, cache

    async def gather(self, user_id: int, now: datetime) -> str | None:
        if not await self.cache.is_active(user_id, Capability.GMAIL):
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
            return None
        worth = []
        for m in (normalize_email(x) for x in extract_messages(res.data)):
            security = "security" in classify(m)
            noise = m["list_unsubscribe"] or any(w in m["from_address"] for w in AUTOMATED)
            if security or not noise:
                worth.append(m)
        if not worth:
            return INBOX_EMPTY
        lines = [f"- {m['from_name']}: {m['subject']}" for m in worth[:5]]
        return f"Inbox: {len(worth)} unread worth a look\n" + "\n".join(lines)

    async def items(self, user_id: int, start: datetime, end: datetime) -> list[BriefItem]:
        """Phase 3 BriefSource protocol (routines.register_brief_source)."""
        text = await self.gather(user_id, start)
        if not text:
            return []
        return [BriefItem(text, text == INBOX_EMPTY)]  # provider content is untrusted
