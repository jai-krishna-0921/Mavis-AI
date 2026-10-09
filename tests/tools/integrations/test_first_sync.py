# ruff: noqa: E501
from mavis.domain.events import EventType, Trust
from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability
from mavis.tools.integrations.first_sync import FirstSync
from tests.tools.integrations.fakes import NOW


class FakeMemory:
    def __init__(self):
        self.calls = []

    async def learn(self, user_id, text, source_ref):
        self.calls.append((user_id, text, source_ref))


class FakeLoops:
    def __init__(self):
        self.upserts = []

    async def upsert(self, user_id, loop):
        self.upserts.append((user_id, loop))
        return loop


async def tz_of(user_id):
    return "Asia/Kolkata"


def make(provider, fake_bus):
    mem, loops = FakeMemory(), FakeLoops()
    return (
        FirstSync(provider=provider, memory=mem, loops=loops, bus=fake_bus, tz_of=tz_of, clock=lambda: NOW),
        mem,
        loops,
    )


EMAILS = {
    "messages": [
        {
            "messageId": "1",
            "threadId": "a",
            "sender": "Jawahar <jawahar@example.com>",
            "subject": "Referral for the Siemens role?",
            "messageText": "Can you send your resume?",
            "labelIds": ["INBOX", "UNREAD"],
            "messageTimestamp": "2026-10-04T08:00:00Z",
        },
        {
            "messageId": "2",
            "threadId": "b",
            "sender": "Google <no-reply@accounts.google.com>",
            "subject": "Security alert",
            "messageText": "New sign-in",
            "labelIds": ["INBOX"],
            "messageTimestamp": "2026-10-04T09:00:00Z",
        },
        {
            "messageId": "3",
            "threadId": "c",
            "sender": "Me <me@example.com>",
            "subject": "Notes",
            "messageText": "fyi",
            "labelIds": ["SENT"],
            "messageTimestamp": "2026-10-04T10:00:00Z",
        },
    ]
}


async def test_gmail_first_sync(provider, fake_bus):
    provider.results["mail.search"] = ToolResult(ok=True, data=EMAILS)
    sync, mem, loops = make(provider, fake_bus)
    noticed = await sync.run(1, Capability.GMAIL)
    assert len(mem.calls) == 1 and "Referral for the Siemens role?" in mem.calls[0][1]
    assert mem.calls[0][2] == "first_sync:1:gmail:0"
    assert loops.upserts == []
    assert 1 <= len(noticed) <= 3 and any("security" in n.lower() for n in noticed)
    [ev] = fake_bus.events
    assert ev.type is EventType.TASK_COMPLETED and ev.id == "first_sync:1:gmail"
    assert ev.payload["kind"] == "first_sync" and ev.payload["noticed"] == noticed
    assert ev.trust is Trust.UNTRUSTED
    assert any("waiting on you" in n for n in noticed)
    assert "\u2014" not in mem.calls[0][1] and "\u2013" not in mem.calls[0][1]


async def test_calendar_first_sync_creates_no_loops(provider, fake_bus):
    provider.results["calendar.list"] = ToolResult(
        ok=True,
        data={
            "items": [
                {
                    "id": "e1",
                    "summary": "Interview with Siemens",
                    "start": {"dateTime": "2026-10-06T10:00:00+05:30"},
                    "attendees": [{"email": "hr@siemens.com"}],
                },
                {"id": "e2", "summary": "Gym", "start": {"dateTime": "2026-10-06T18:00:00+05:30"}},
            ]
        },
    )
    sync, mem, loops = make(provider, fake_bus)
    noticed = await sync.run(1, Capability.CALENDAR)
    assert loops.upserts == []
    assert len(mem.calls) == 1 and mem.calls[0][2] == "first_sync:1:calendar:0"
    assert [ev.trust for ev in fake_bus.events] == [Trust.UNTRUSTED]
    assert noticed[0].startswith("Next up: Interview with Siemens, Tue 06 Oct 10:00")


async def test_failed_provider_call_still_completes(provider, fake_bus):
    provider.results["mail.search"] = ToolResult(ok=False, error="boom")
    sync, mem, loops = make(provider, fake_bus)
    assert await sync.run(1, Capability.GMAIL) == []
    assert fake_bus.events[0].payload["noticed"] == []
    assert fake_bus.events[0].trust is Trust.UNTRUSTED


async def test_learn_jobs_are_capped_at_three_batches(fake_bus):
    from mavis.tools.integrations.first_sync import MAX_LEARN_JOBS

    mem = FakeMemory()
    sync = FirstSync(
        provider=None, memory=mem, loops=FakeLoops(), bus=fake_bus, tz_of=tz_of, clock=lambda: NOW
    )
    lines = [f"line {i}" for i in range(50)]
    await sync._learn_batches(1, "Header:", lines, "first_sync:1:gmail")
    assert len(mem.calls) <= MAX_LEARN_JOBS == 3
    joined = "\n".join(text for _, text, _ in mem.calls)
    assert all(f"line {i}" in joined for i in range(50))  # combined, nothing dropped
    refs = [ref for _, _, ref in mem.calls]
    assert refs == ["first_sync:1:gmail:0", "first_sync:1:gmail:1", "first_sync:1:gmail:2"]


async def test_few_lines_make_one_job(fake_bus):
    mem = FakeMemory()
    sync = FirstSync(
        provider=None, memory=mem, loops=FakeLoops(), bus=fake_bus, tz_of=tz_of, clock=lambda: NOW
    )
    await sync._learn_batches(1, "H:", ["a", "b"], "r")
    assert len(mem.calls) == 1
    await sync._learn_batches(1, "H:", [], "r")
    assert len(mem.calls) == 1


async def test_gmail_and_calendar_share_the_three_job_budget(provider, fake_bus):
    from mavis.tools.integrations.first_sync import CALENDAR_LEARN_JOBS, GMAIL_LEARN_JOBS, MAX_LEARN_JOBS

    assert GMAIL_LEARN_JOBS + CALENDAR_LEARN_JOBS == MAX_LEARN_JOBS == 3
    mem = FakeMemory()
    sync = FirstSync(
        provider=provider, memory=mem, loops=FakeLoops(), bus=fake_bus, tz_of=tz_of, clock=lambda: NOW
    )
    lines = [f"l{i}" for i in range(60)]
    await sync._learn_batches(1, "H:", lines, "g", GMAIL_LEARN_JOBS)
    await sync._learn_batches(1, "H:", lines, "c", CALENDAR_LEARN_JOBS)
    assert len(mem.calls) == 3


async def test_learn_refs_are_scoped_per_user(provider, fake_bus):
    provider.results["mail.search"] = ToolResult(ok=True, data=EMAILS)
    sync, mem, _ = make(provider, fake_bus)
    await sync.run(1, Capability.GMAIL)
    await sync.run(2, Capability.GMAIL)
    refs = {(u, ref) for u, _, ref in mem.calls}
    assert (1, "first_sync:1:gmail:0") in refs and (2, "first_sync:2:gmail:0") in refs
    assert len({ref for _, ref in refs}) == len(refs)


class FakeConnectors:
    def __init__(self):
        self.mail, self.slack_msgs = [], []

    async def email(self, user_id, n):
        self.mail.append(n)
        return True

    async def slack(self, user_id, n):
        self.slack_msgs.append(n)
        return True

    async def on_slack_event(self, event):
        await self.slack(event.user_id, event.payload)


def windows(monkeypatch, **kw):
    from types import SimpleNamespace

    from mavis.tools.integrations import first_sync

    monkeypatch.setattr(first_sync, "get_settings", lambda: SimpleNamespace(**kw))


async def test_gmail_first_sync_reads_the_settings_window_and_learns_per_record(provider, fake_bus, monkeypatch):
    windows(monkeypatch, sync_gmail_days=3)
    provider.results["mail.search"] = ToolResult(ok=True, data=EMAILS)
    sync, mem, _ = make(provider, fake_bus)
    sync.connectors = FakeConnectors()
    await sync.run(1, Capability.GMAIL)
    [(_, _, args)] = [c for c in provider.executed if c[1] == "mail.search"]
    assert args["query"].startswith("newer_than:3d ")
    assert [m["message_id"] for m in sync.connectors.mail] == ["1", "2", "3"]  # every message, filtered by the guard
    assert mem.calls == []  # no batch of one-liners: each record is its own LEARN job


async def test_calendar_first_sync_window_is_a_week_back_and_a_month_ahead(provider, fake_bus):
    provider.results["calendar.list"] = ToolResult(ok=True, data={"items": []})
    sync, _, _ = make(provider, fake_bus)
    await sync.run(1, Capability.CALENDAR)
    [(_, _, args)] = [c for c in provider.executed if c[1] == "calendar.list"]
    from datetime import datetime

    lo, hi = datetime.fromisoformat(args["time_min"]), datetime.fromisoformat(args["time_max"])
    assert (NOW - lo).days == 7 and (hi - NOW).days == 30


async def test_slack_first_sync_reads_only_recent_messages_of_member_channels(provider, fake_bus, monkeypatch):
    windows(monkeypatch, sync_slack_days=2)
    fresh, old = NOW.timestamp() - 3600, NOW.timestamp() - 5 * 86400
    provider.results["slack.channels"] = ToolResult(ok=True, data={"channels": [{"id": "C0GEN0001", "name": "general"}]})
    provider.results["slack.history"] = ToolResult(ok=True, data={"messages": [
        {"ts": f"{fresh:.6f}", "user": "U0ARJUN01", "text": "fresh"},
        {"ts": f"{old:.6f}", "user": "U0ARJUN01", "text": "stale"}]})
    sync, _, _ = make(provider, fake_bus)
    sync.connectors = FakeConnectors()
    await sync.run(1, Capability.SLACK)
    assert [m["text"] for m in sync.connectors.slack_msgs] == ["fresh"]
    assert sync.connectors.slack_msgs[0]["channel"] == "C0GEN0001"
