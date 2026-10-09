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
    assert await act.on_active(1, Capability.NOTION) is False
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


async def test_transient_status_error_keeps_chain_alive(provider, cache, fake_bus, state, rec):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["mail.search"] = ToolResult(ok=True, data={"messages": [RAW]})
    await state.update(1, {"polling": {"gmail": True}})
    real = provider.status
    calls = {"n": 0}

    async def flaky(user):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real(user)

    provider.status = flaky
    poller = make_poller(provider, cache, fake_bus, state, rec)
    assert await poller.poll(1, Capability.GMAIL) == 0
    assert rec.scheduled == [(1, NOW + POLL_INTERVAL, "gmail", POLL_KIND)]
    assert await poller.poll(1, Capability.GMAIL) == 1
    assert len(rec.scheduled) == 2


async def test_gmail_cursor_ignores_missing_timestamp_and_clamps(provider, cache, fake_bus, state, rec):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    future = dict(RAW, messageId="m2", messageTimestamp="2026-10-05T09:00:00Z")
    nots = {k: v for k, v in RAW.items() if k != "messageTimestamp"} | {"messageId": "m3"}
    provider.results["mail.search"] = ToolResult(ok=True, data={"messages": [nots]})
    await state.update(1, {"polling": {"gmail": True}})
    poller = make_poller(provider, cache, fake_bus, state, rec)
    await poller.poll(1, Capability.GMAIL)
    assert (await state.get(1))["cursors"]["gmail_after"] == int((NOW - timedelta(minutes=10)).timestamp())
    provider.results["mail.search"] = ToolResult(ok=True, data={"messages": [future]})
    await poller.poll(1, Capability.GMAIL)
    assert (await state.get(1))["cursors"]["gmail_after"] == int(NOW.timestamp())


class StaleCache:
    """Another process cached 'not active' for this user; only fresh=True tells the truth."""

    def __init__(self, truth):
        self.truth, self.fresh_calls = truth, 0

    async def is_active(self, user_id, capability, *, fresh=False):
        return (await self.status(user_id, fresh=fresh)).get(capability.value) is ConnectionState.ACTIVE

    async def status(self, user_id, *, fresh=False):
        if fresh:
            self.fresh_calls += 1
            return {c.value: s for c, s in self.truth.items()}
        return {c.value: ConnectionState.INITIATED for c in self.truth}


async def test_stale_cache_does_not_kill_the_chain(provider, fake_bus, state, rec):
    cache = StaleCache({Capability.GMAIL: ConnectionState.ACTIVE})
    poller = make_poller(provider, cache, fake_bus, state, rec)
    await state.update(1, {"polling": {"gmail": True}})
    await poller.poll(1, Capability.GMAIL)
    assert cache.fresh_calls == 1 and len(provider.executed) == 1
    assert rec.scheduled == [(1, NOW + POLL_INTERVAL, "gmail", POLL_KIND)]


async def test_state_read_failure_keeps_the_chain(provider, cache, fake_bus, state, rec):
    poller = make_poller(provider, cache, fake_bus, state, rec)

    async def boom(user_id):
        raise RuntimeError("db down")

    state.get = boom
    assert await poller.poll(1, Capability.GMAIL) == 0
    assert rec.scheduled == [(1, NOW + POLL_INTERVAL, "gmail", POLL_KIND)]


async def test_ensure_chains_rearms_every_polling_capability(provider, cache, fake_bus, state, rec):
    poller = make_poller(provider, cache, fake_bus, state, rec)
    await state.update(1, {"polling": {"gmail": True, "googlecalendar": False, "slack": True}})
    assert await poller.ensure_chains(1) == 1  # slack is not pollable, calendar polling is off
    assert rec.scheduled == [(1, NOW, "gmail", POLL_KIND)]
    await state.update(2, {})
    assert await poller.ensure_chains(2) == 0


async def test_ensure_all_chains_covers_every_user(provider, cache, fake_bus, state, rec):
    async def ids():
        return [1, 2, 3]

    poller = Poller(provider=provider, cache=cache, bus=fake_bus, state=state, schedule=rec.schedule,
                    user_ids=ids, clock=lambda: NOW)
    await state.update(1, {"polling": {"gmail": True}})
    await state.update(3, {"polling": {"googlecalendar": True}})
    assert await poller.ensure_all_chains() == 2
    assert sorted((u, r) for u, _, r, _ in rec.scheduled) == [(1, "gmail"), (3, "googlecalendar")]


async def test_failed_connection_prompts_once_and_stops_the_chain(provider, cache, fake_bus, state, rec):
    prompts = []

    async def on_failed(user_id, capability):
        prompts.append((user_id, capability))

    poller = Poller(provider=provider, cache=cache, bus=fake_bus, state=state, schedule=rec.schedule,
                    on_failed=on_failed, clock=lambda: NOW)
    provider.set_state(1, Capability.GMAIL, ConnectionState.FAILED)
    await state.update(1, {"polling": {"gmail": True}})
    assert await poller.poll(1, Capability.GMAIL) == 0
    assert prompts == [(1, Capability.GMAIL)]
    assert rec.scheduled == [] and "gmail" not in (await state.get(1))["polling"]
    assert await poller.poll(1, Capability.GMAIL) == 0  # chain is over: no second prompt
    assert len(prompts) == 1


async def test_failed_prompt_error_keeps_chain_for_retry(provider, cache, fake_bus, state, rec):
    async def on_failed(user_id, capability):
        raise RuntimeError("outbox down")

    poller = Poller(provider=provider, cache=cache, bus=fake_bus, state=state, schedule=rec.schedule,
                    on_failed=on_failed, clock=lambda: NOW)
    provider.set_state(1, Capability.GMAIL, ConnectionState.FAILED)
    await state.update(1, {"polling": {"gmail": True}})
    await poller.poll(1, Capability.GMAIL)
    assert (await state.get(1))["polling"]["gmail"] is True
    assert rec.scheduled == [(1, NOW + POLL_INTERVAL, "gmail", POLL_KIND)]


async def test_auth_error_with_failed_status_prompts_and_stops(provider, fake_bus, state, rec):
    prompts = []

    async def on_failed(user_id, capability):
        prompts.append(capability)

    class Cache:  # the cached view says ACTIVE; the fresh view (after the 401) says FAILED
        async def is_active(self, user_id, capability, *, fresh=False):
            return True

        async def status(self, user_id, *, fresh=False):
            return {"gmail": ConnectionState.FAILED if fresh else ConnectionState.ACTIVE}

    poller = Poller(provider=provider, cache=Cache(), bus=fake_bus, state=state, schedule=rec.schedule,
                    on_failed=on_failed, clock=lambda: NOW)
    await state.update(1, {"polling": {"gmail": True}})
    provider.results["mail.search"] = ToolResult(ok=False, error="Composio answered 401 for POST /tools")
    await poller.poll(1, Capability.GMAIL)
    assert prompts == [Capability.GMAIL] and rec.scheduled == []


async def test_auth_error_while_provider_says_active_keeps_polling(provider, cache, fake_bus, state, rec):
    poller = make_poller(provider, cache, fake_bus, state, rec)
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["mail.search"] = ToolResult(ok=False, error="Composio answered 403 for POST /tools")
    await state.update(1, {"polling": {"gmail": True}})
    await poller.poll(1, Capability.GMAIL)
    assert rec.scheduled == [(1, NOW + POLL_INTERVAL, "gmail", POLL_KIND)]
