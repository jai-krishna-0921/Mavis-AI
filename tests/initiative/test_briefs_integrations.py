from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import Capability
from mavis.initiative.briefs_integrations import CalendarBrief, InboxBrief
from mavis.initiative.routines import BriefItem
from mavis.tools.integrations.connections import ConnectionCache
from tests.tools.integrations.fakes import NOW, FakeProvider


async def tz_of(user_id):
    return "Asia/Kolkata"


async def test_calendar_brief_lists_today_in_user_tz():
    p = FakeProvider()
    p.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)
    p.results["calendar.list"] = ToolResult(
        ok=True,
        data={
            "items": [
                {
                    "id": "e1",
                    "summary": "Interview prep",
                    "start": {"dateTime": "2026-10-05T04:30:00Z"},
                    "attendees": [{"email": "jawahar@example.com"}],
                },
            ]
        },
    )
    text = await CalendarBrief(p, ConnectionCache(p), tz_of).gather(1, NOW)
    assert text == "Calendar today:\n- 10:00 Interview prep (with jawahar@example.com)"
    args = p.executed[0][2]
    assert args["time_min"] == "2026-10-05T00:00:00+05:30" and args["time_max"] == "2026-10-06T00:00:00+05:30"


async def test_brief_items_adapter():
    p = FakeProvider()
    assert await CalendarBrief(p, ConnectionCache(p), tz_of).items(1, NOW, NOW) == []
    p.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)
    p.results["calendar.list"] = ToolResult(ok=True, data={"items": []})
    assert await CalendarBrief(p, ConnectionCache(p), tz_of).items(1, NOW, NOW) == [
        BriefItem("Calendar today: nothing scheduled.", True)
    ]


async def test_calendar_items_with_provider_content_are_untrusted():
    p = FakeProvider()
    p.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)
    p.results["calendar.list"] = ToolResult(
        ok=True,
        data={"items": [{"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-10-05T04:30:00Z"}}]},
    )
    [item] = await CalendarBrief(p, ConnectionCache(p), tz_of).items(1, NOW, NOW)
    assert item == BriefItem("Calendar today:\n- 10:00 Standup", False)


async def test_calendar_brief_none_when_not_connected():
    p = FakeProvider()
    assert await CalendarBrief(p, ConnectionCache(p), tz_of).gather(1, NOW) is None


async def test_calendar_brief_empty_day():
    p = FakeProvider()
    p.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)
    p.results["calendar.list"] = ToolResult(ok=True, data={"items": []})
    assert (
        await CalendarBrief(p, ConnectionCache(p), tz_of).gather(1, NOW)
        == "Calendar today: nothing scheduled."
    )


def inbox_provider() -> FakeProvider:
    p = FakeProvider()
    p.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    p.results["mail.search"] = ToolResult(
        ok=True,
        data={
            "messages": [
                {
                    "messageId": "1",
                    "sender": "Jawahar <j@example.com>",
                    "subject": "Referral?",
                    "labelIds": ["UNREAD"],
                },
                {
                    "messageId": "2",
                    "sender": "Medium <noreply@medium.com>",
                    "subject": "Digest",
                    "labelIds": ["UNREAD"],
                    "payload": {"headers": [{"name": "List-Unsubscribe", "value": "x"}]},
                },
                {
                    "messageId": "3",
                    "sender": "Google <no-reply@accounts.google.com>",
                    "subject": "Security alert",
                    "labelIds": ["UNREAD"],
                },
            ]
        },
    )
    return p


async def test_inbox_brief_skips_newsletters_keeps_security():
    p = inbox_provider()
    text = await InboxBrief(p, ConnectionCache(p)).gather(1, NOW)
    assert text.startswith("Inbox: 2 unread worth a look")
    assert "Jawahar: Referral?" in text and "Security alert" in text and "Digest" not in text
    assert "—" not in text and "–" not in text


async def test_inbox_items_trust_flags():
    p = inbox_provider()
    [item] = await InboxBrief(p, ConnectionCache(p)).items(1, NOW, NOW)
    assert item.trusted is False
    p.results["mail.search"] = ToolResult(ok=True, data={"messages": []})
    assert await InboxBrief(p, ConnectionCache(p)).items(1, NOW, NOW) == [
        BriefItem("Inbox: nothing unread that needs you.", True)
    ]


async def test_failed_connection_prompts_reconnect_and_brief_skips_source():
    p = FakeProvider()
    p.set_state(1, Capability.GMAIL, ConnectionState.FAILED)
    prompts = []

    async def on_failed(user_id, capability):
        prompts.append((user_id, capability))

    assert await InboxBrief(p, ConnectionCache(p), on_failed=on_failed).gather(1, NOW) is None
    assert prompts == [(1, Capability.GMAIL)]


async def test_brief_survives_a_failing_reconnect_prompt():
    p = FakeProvider()
    p.set_state(1, Capability.CALENDAR, ConnectionState.FAILED)

    async def on_failed(user_id, capability):
        raise RuntimeError("outbox down")

    assert await CalendarBrief(p, ConnectionCache(p), tz_of, on_failed=on_failed).gather(1, NOW) is None


async def test_brief_auth_error_confirmed_failed_prompts():
    p = FakeProvider()
    p.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    p.results["mail.search"] = ToolResult(ok=False, error="Composio answered 401 for POST /tools/execute/x")
    prompts = []

    async def on_failed(user_id, capability):
        prompts.append(capability)

    cache = ConnectionCache(p)
    brief = InboxBrief(p, cache, on_failed=on_failed)
    assert await brief.gather(1, NOW) is None and prompts == []  # provider still says ACTIVE
    p.set_state(1, Capability.GMAIL, ConnectionState.FAILED)
    assert await brief.gather(1, NOW) is None  # cache is warm with ACTIVE: auth error then confirms
    assert prompts == [Capability.GMAIL]
