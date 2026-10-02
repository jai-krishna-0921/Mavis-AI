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
    assert mem.calls[0][2] == "first_sync:gmail:0"
    assert loops.upserts == []
    assert 1 <= len(noticed) <= 3 and any("security" in n.lower() for n in noticed)
    [ev] = fake_bus.events
    assert ev.type is EventType.TASK_COMPLETED and ev.id == "first_sync:1:gmail"
    assert ev.payload["kind"] == "first_sync" and ev.payload["noticed"] == noticed
    assert ev.trust is Trust.UNTRUSTED
    assert any("waiting on you" in n for n in noticed)
    assert "\u2014" not in mem.calls[0][1] and "\u2013" not in mem.calls[0][1]


async def test_calendar_first_sync_creates_commitments(provider, fake_bus):
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
    assert len(mem.calls) == 1 and mem.calls[0][2] == "first_sync:calendar:0"
    assert [ev.trust for ev in fake_bus.events] == [Trust.UNTRUSTED]
    assert noticed[0].startswith("Next up: Interview with Siemens, Tue 06 Oct 10:00")


async def test_failed_provider_call_still_completes(provider, fake_bus):
    provider.results["mail.search"] = ToolResult(ok=False, error="boom")
    sync, mem, loops = make(provider, fake_bus)
    assert await sync.run(1, Capability.GMAIL) == []
    assert fake_bus.events[0].payload["noticed"] == []
    assert fake_bus.events[0].trust is Trust.UNTRUSTED
