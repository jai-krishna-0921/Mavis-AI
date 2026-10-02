"""On a new connection: skim recent history so Mavis is useful from minute one (spec §6.4).

Third-party content never creates loops here (Phase 3 ruling). `loops` is kept for a later phase
and is unused. Learning goes through `memory.learn`, which in production is an untrusted LEARN-job
adapter. The TASK_COMPLETED event is UNTRUSTED because `noticed` carries email subjects.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.integrations import UserRef
from mavis.domain.loops import LoopUpsert
from mavis.domain.policy import Capability
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.normalize import (
    extract_calendar_items,
    extract_list,
    extract_messages,
    normalize_calendar_event,
    normalize_email,
    pick,
    to_datetime,
)

BATCH = 15  # lines per LEARN job, until MAX_LEARN_JOBS binds
MAX_LEARN_JOBS = 3  # first sync must not flood the single LLM slot: lines are combined into <= 3 jobs
MAX_REPLY_LOOPS = 5
AUTOMATED = ("no-reply", "noreply", "notifications", "mailer-daemon", "donotreply", "do-not-reply")
SECURITY_WORDS = ("security alert", "new sign-in", "suspicious", "unusual activity")


class Learner(Protocol):
    async def learn(self, user_id: int, text: str, source_ref: str) -> Any: ...


class LoopWriter(Protocol):
    async def upsert(self, user_id: int, loop: LoopUpsert) -> Any: ...


class FirstSync:
    def __init__(
        self,
        *,
        provider: IntegrationProvider,
        memory: Learner,
        loops: LoopWriter,
        bus: EventBus,
        tz_of: Callable[[int], Awaitable[str]],
        clock: Callable[[], datetime] = timeutil.now,
    ) -> None:
        self.provider, self.memory, self.loops, self.bus = provider, memory, loops, bus
        self.tz_of, self.clock = tz_of, clock

    async def run(self, user_id: int, capability: Capability) -> list[str]:
        handlers = {
            Capability.GMAIL: self._gmail,
            Capability.CALENDAR: self._calendar,
            Capability.SLACK: self._slack,
            Capability.NOTION: self._notion,
        }
        noticed = (await handlers[capability](user_id))[:3]
        await self.bus.publish(
            Event(
                id=f"first_sync:{user_id}:{capability.value}",
                user_id=user_id,
                type=EventType.TASK_COMPLETED,
                occurred_at=self.clock(),
                source="integrations",
                trust=Trust.UNTRUSTED,
                payload={"kind": "first_sync", "capability": capability.value, "noticed": noticed},
            )
        )
        return noticed

    async def _execute(self, user_id: int, action: str, args: dict) -> Any:
        res = await self.provider.execute(UserRef(user_id=user_id), action, args)
        return res.data if res.ok else None

    async def _learn_batches(self, user_id: int, header: str, lines: list[str], ref: str) -> None:
        size = max(BATCH, -(-len(lines) // MAX_LEARN_JOBS))  # ceil: never more than MAX_LEARN_JOBS chunks
        for n, i in enumerate(range(0, len(lines), size)):
            chunk = "\n".join(lines[i : i + size])
            await self.memory.learn(user_id, f"{header}\n{chunk}", f"{ref}:{n}")

    async def _gmail(self, user_id: int) -> list[str]:
        data = await self._execute(
            user_id,
            "mail.search",
            {
                "query": "newer_than:14d -category:promotions -category:social",
                "max_results": 50,
            },
        )
        if data is None:
            return []
        mails = [normalize_email(m) for m in extract_messages(data)]
        lines = [
            f"Email from {m['from']} ({(m['received_at'] or '')[:10]}): {m['subject']}: {m['snippet'][:160]}"
            for m in mails
            if "SENT" not in m["labels"]
        ]
        await self._learn_batches(
            user_id,
            "Recent emails (untrusted content; extract people, organisations and events only):",
            lines,
            "first_sync:gmail",
        )
        latest_by_thread: dict[str, dict] = {}
        for m in sorted(mails, key=lambda x: x["received_at"] or ""):
            latest_by_thread[m["thread_id"] or m["message_id"]] = m
        reply_loops, first_waiting = 0, ""
        for m in latest_by_thread.values():
            if reply_loops >= MAX_REPLY_LOOPS:
                break
            human = not any(word in m["from_address"] for word in AUTOMATED)
            waiting = (
                "INBOX" in m["labels"]
                and "SENT" not in m["labels"]
                and ("UNREAD" in m["labels"] or "?" in m["subject"] + m["snippet"])
            )
            if human and waiting:
                reply_loops += 1
                first_waiting = first_waiting or m["subject"]
        noticed: list[str] = []
        if reply_loops:
            noticed.append(
                f'{reply_loops} thread(s) look like they\'re waiting on you, e.g. "{first_waiting}"'
            )
        security = [
            m for m in mails if any(w in (m["subject"] + m["snippet"]).lower() for w in SECURITY_WORDS)
        ]
        if security:
            noticed.append(
                f"{len(security)} security alert(s) in the last two weeks; "
                f'latest: "{security[-1]["subject"]}"'
            )
        if lines:
            noticed.append(f"Skimmed {len(lines)} recent emails to learn who you talk to")
        return noticed

    async def _calendar(self, user_id: int) -> list[str]:
        now = self.clock()
        data = await self._execute(
            user_id,
            "calendar.list",
            {
                "time_min": now.isoformat(),
                "time_max": (now + timedelta(days=14)).isoformat(),
                "max_results": 50,
            },
        )
        if data is None:
            return []
        tz = ZoneInfo(await self.tz_of(user_id))
        events = [normalize_calendar_event(e) for e in extract_calendar_items(data)]
        lines = []
        for e in events:
            start = to_datetime(e["start"])
            when = f"{start.astimezone(tz):%a %d %b %H:%M}" if start else "time unknown"
            lines.append(
                f"Calendar: {e['summary']} on {when}"
                + (f" with {', '.join(e['attendees'])}" if e["attendees"] else "")
            )
        await self._learn_batches(user_id, "Upcoming calendar:", lines, "first_sync:calendar")
        noticed: list[str] = []
        timed = sorted(((to_datetime(e["start"]), e) for e in events if e["start"]), key=lambda t: t[0])
        if timed:
            start, e = timed[0]
            noticed.append(f"Next up: {e['summary']}, {start.astimezone(tz):%a %d %b %H:%M}")
        if events:
            noticed.append(f"{len(events)} event(s) in the next two weeks")
        return noticed

    async def _slack(self, user_id: int) -> list[str]:
        data = await self._execute(user_id, "slack.channels", {})
        if data is None:
            return []
        names = [str(pick(c, "name", default="")) for c in extract_list(data, "channels", "data.channels")]
        names = [n for n in names if n]
        if names:
            await self.memory.learn(
                user_id, "Slack channels the user is in: " + ", ".join(names[:50]), "first_sync:slack:0"
            )
        return [f"You're in {len(names)} Slack channels"] if names else []

    async def _notion(self, user_id: int) -> list[str]:
        data = await self._execute(user_id, "notion.search", {"query": ""})
        if data is None:
            return []
        titles = [
            str(pick(p, "title", "properties.title.title.0.plain_text", default=""))
            for p in extract_list(data, "results", "data.results", "pages")
        ]
        titles = [t for t in titles if t]
        if titles:
            await self.memory.learn(
                user_id, "Notion pages the user keeps: " + "; ".join(titles[:50]), "first_sync:notion:0"
            )
        return [f"Found {len(titles)} Notion pages"] if titles else []
