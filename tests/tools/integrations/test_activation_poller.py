import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta

from mavis.domain.integrations import ConnectionState, ToolResult
from mavis.domain.policy import Capability
from mavis.tools.integrations.activation import Activator
from mavis.tools.integrations.composio_webhooks import parse_composio_webhook
from mavis.tools.integrations.normalize import email_event
from mavis.tools.integrations.poller import POLL_INTERVAL, POLL_KIND, Poller
from tests.tools.integrations.fakes import NOW

RAW = {"messageId": "m1", "threadId": "t1", "sender": "Google <no-reply@accounts.google.com>",
       "subject": "Security alert", "messageText": "New sign-in on Windows", "labelIds": ["INBOX", "UNREAD"],
       "messageTimestamp": "2026-10-05T04:25:00Z"}  # within the 10-minute initial lookback of NOW


async def test_activation_subscribes_triggers(provider, state, rec):
    act = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                    clock=lambda: NOW)
    assert await act.on_active(1, Capability.GMAIL) is False
    assert provider.subscribed == [(1, "mail.new_message")]
    assert (await state.get(1))["polling"] == {"gmail": False}
    assert rec.scheduled == []


async def test_activation_falls_back_to_polling(provider, state, rec):
    provider.fail_subscribe = True
    act = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                    clock=lambda: NOW)
    assert await act.on_active(1, Capability.CALENDAR) is True
    assert (await state.get(1))["polling"] == {"googlecalendar": True}
    assert rec.scheduled == [(1, NOW, "googlecalendar", POLL_KIND)]


async def test_activation_forced_polling_skips_subscribe(provider, state, rec):
    act = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=True,
                    clock=lambda: NOW)
    assert await act.on_active(1, Capability.GMAIL) is True
    assert provider.subscribed == []


async def test_activation_unpollable_capability_never_polls(provider, state, rec):
    provider.fail_subscribe = True
    act = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                    clock=lambda: NOW)
    assert await act.on_active(1, Capability.SLACK) is False
    assert rec.scheduled == []


def make_poller(provider, cache, fake_bus, state, rec):
    return Poller(provider=provider, cache=cache, bus=fake_bus, state=state, schedule=rec.schedule,
                  clock=lambda: NOW)


async def test_gmail_poll_emits_and_advances_cursor(provider, cache, fake_bus, state, rec):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["mail.search"] = ToolResult(ok=True, data={"messages": [RAW]})
    await state.update(1, {"polling": {"gmail": True}})
    poller = make_poller(provider, cache, fake_bus, state, rec)
    assert await poller.poll(1, Capability.GMAIL) == 1
    after = int((NOW - timedelta(minutes=10)).timestamp())
    assert provider.executed[0][2]["query"] == f"after:{after} -in:sent"
    assert (await state.get(1))["cursors"]["gmail_after"] == int(
        email_event(1, RAW, "x").occurred_at.timestamp()
    )
    assert rec.scheduled == [(1, NOW + POLL_INTERVAL, "gmail", POLL_KIND)]


async def test_second_poll_does_not_republish(provider, cache, fake_bus, state, rec):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["mail.search"] = ToolResult(ok=True, data={"messages": [RAW]})
    await state.update(1, {"polling": {"gmail": True}})
    poller = make_poller(provider, cache, fake_bus, state, rec)
    await poller.poll(1, Capability.GMAIL)
    assert await poller.poll(1, Capability.GMAIL) == 0
    assert len(fake_bus.events) == 1


async def test_poll_stops_when_disconnected_or_disabled(provider, cache, fake_bus, state, rec):
    poller = make_poller(provider, cache, fake_bus, state, rec)
    await state.update(1, {"polling": {"gmail": True}})
    assert await poller.poll(1, Capability.GMAIL) == 0          # not ACTIVE
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    cache.invalidate(1)
    await state.update(1, {"polling": {"gmail": False}})
    await poller.on_wakeup(1, "gmail")
    assert rec.scheduled == [] and provider.executed == []


async def test_calendar_poll_uses_updated_min(provider, cache, fake_bus, state, rec):
    provider.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)
    provider.results["calendar.list"] = ToolResult(ok=True, data={"items": [
        {"id": "e1", "summary": "Interview prep", "updated": "2026-10-05T03:00:00Z",
         "start": {"dateTime": "2026-10-06T10:00:00+05:30"}},
    ]})
    await state.update(1, {"polling": {"googlecalendar": True}})
    poller = make_poller(provider, cache, fake_bus, state, rec)
    assert await poller.poll(1, Capability.CALENDAR) == 1
    args = provider.executed[0][2]
    assert args["updated_min"] == (NOW - timedelta(minutes=10)).isoformat()
    assert (await state.get(1))["cursors"]["gcal_updated_min"] == NOW.isoformat()


def test_poller_and_webhook_produce_identical_email_events():
    polled = email_event(1, RAW, source="poller")
    meta = {"trigger_slug": "GMAIL_NEW_GMAIL_MESSAGE", "user_id": "mavis-1"}
    body = json.dumps({"id": "w1", "metadata": meta, "data": RAW}).encode()
    ts = str(int(time.time()))
    msg = f"w1.{ts}.{body.decode()}".encode()
    sig = base64.b64encode(hmac.new(b"s", msg, hashlib.sha256).digest()).decode()
    [pushed] = parse_composio_webhook(
        {"webhook-id": "w1", "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}"}, body, "s"
    )
    assert (polled.id, polled.type, polled.payload, polled.trust) == (
        pushed.id, pushed.type, pushed.payload, pushed.trust
    )
