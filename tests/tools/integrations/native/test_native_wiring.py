# ruff: noqa: E501
"""The pieces joined: Slack events find their Mavis user, an ACTIVE native grant is polled (never left to
triggers), first sync reads the window through the guard, and every inbound path reaches the record
ingest once per record."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mavis.attention.connector_ingest import ConnectorIngest, remember_identity
from mavis.domain.events import EventType
from mavis.domain.policy import Capability
from mavis.store.repo import users
from mavis.tools.integrations import wiring
from mavis.tools.integrations.activation import Activator
from mavis.tools.integrations.first_sync import FirstSync
from mavis.tools.integrations.native import slack_events
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.router import NativeRouter
from mavis.tools.integrations.native.tokens import REVOKED
from mavis.tools.integrations.normalize import email_event, slack_event
from mavis.tools.integrations.poller import POLL_KIND, Poller
from tests.tools.integrations.fakes import NOW, FakeBus, FakeProvider
from tests.tools.integrations.native.conftest import *  # noqa: F403 - fixtures
from tests.tools.integrations.test_native_slack_sync import SlackProvider, chans, m

G, S = NativeProvider.GOOGLE, NativeProvider.SLACK
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.events"]


async def make_users(n):
    return [(await users.get_or_create_by_chat(900 + i, f"User {i}"))[0] for i in range(n)]


async def grant_slack(tokens, user_id, team="T1", slack_user="U1"):
    await tokens.save(user_id, S, account={"team_id": team, "user_id": slack_user, "scopes": ["chat:write"]},
                      access_token="xoxp-1", refresh_token=None, expires_at=None)


async def grant_google(tokens, user_id, email):
    await tokens.save(user_id, G, account={"email": email, "scopes": GOOGLE_SCOPES}, access_token="ya29.a",
                      refresh_token="1//r", expires_at=None)


# --- 1. Slack user lookup --------------------------------------------------------------------------------


async def test_the_lookup_finds_the_active_grant_of_that_slack_user_in_that_team(tokens):
    a, b, c = await make_users(3)
    await grant_slack(tokens, a.id, "T1", "U1")
    await grant_slack(tokens, b.id, "T1", "U2")
    await grant_slack(tokens, c.id, "T2", "U1")  # the same Slack id in another workspace
    assert await tokens.user_for_slack("T1", "U1") == a.id
    assert await tokens.user_for_slack("T1", "U2") == b.id
    assert await tokens.user_for_slack("T2", "U1") == c.id
    for team, who in (("T1", "U9"), ("T3", "U1"), ("", "U1"), ("T1", "")):
        assert await tokens.user_for_slack(team, who) is None


async def test_a_revoked_or_deleted_grant_no_longer_receives_events(tokens):
    [a] = await make_users(1)
    await grant_slack(tokens, a.id)
    await tokens.mark(a.id, S, REVOKED)
    assert await tokens.user_for_slack("T1", "U1") is None
    await grant_slack(tokens, a.id)  # reconnect revives it
    assert await tokens.user_for_slack("T1", "U1") == a.id
    await tokens.delete(a.id, S)
    assert await tokens.user_for_slack("T1", "U1") is None


async def test_startup_installs_a_lookup_that_follows_the_configured_provider(tokens, client, monkeypatch):
    [a] = await make_users(1)
    await grant_slack(tokens, a.id)
    router = NativeRouter(FakeProvider(), tokens, None, [], client)
    monkeypatch.setattr(wiring, "get_provider", lambda: router)
    slack_events.set_user_lookup(None)
    wiring.register_integrations()
    lookup = slack_events.get_user_lookup()
    assert lookup is not None and await lookup.user_for_slack("T1", "U1") == a.id
    assert await lookup.user_for_slack("T1", "U404") is None
    monkeypatch.setattr(wiring, "get_provider", lambda: FakeProvider())  # Composio only: nobody maps
    assert await lookup.user_for_slack("T1", "U1") is None
    slack_events.set_user_lookup(None)


# --- 2. native grants are polled -------------------------------------------------------------------------


class NativeAware(FakeProvider):
    def __init__(self, native):
        super().__init__()
        self.native = native

    async def uses_native(self, user_id, capability):
        return (user_id, capability) in self.native


@pytest.mark.parametrize("capability", [Capability.GMAIL, Capability.CALENDAR, Capability.SLACK])
async def test_activation_polls_a_native_grant_even_when_triggers_would_attach(state, rec, capability):
    provider = NativeAware({(1, capability)})
    act = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                    clock=lambda: NOW)
    assert await act.on_active(1, capability) is True
    assert provider.subscribed == []
    assert (await state.get(1))["polling"] == {capability.value: True}
    assert rec.scheduled == [(1, NOW, capability.value, POLL_KIND)]


async def test_activation_still_subscribes_for_a_composio_user(state, rec):
    provider = NativeAware(set())
    act = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                    clock=lambda: NOW)
    assert await act.on_active(1, Capability.GMAIL) is False
    assert provider.subscribed == [(1, "mail.new_message")]


async def test_ensure_chains_arms_every_polled_capability_of_an_active_native_grant(cache, fake_bus, state, rec):
    provider = NativeAware({(1, Capability.SLACK), (1, Capability.GMAIL), (1, Capability.CALENDAR)})
    poller = Poller(provider=provider, cache=cache, bus=fake_bus, state=state, schedule=rec.schedule,
                    clock=lambda: NOW)
    assert await poller.ensure_chains(1) == 3  # no polling flag was ever recorded
    assert {r for _, _, r, _ in rec.scheduled} == {"slack", "gmail", "googlecalendar"}
    assert (await state.get(1))["polling"] == {"slack": True, "gmail": True, "googlecalendar": True}
    assert await poller.ensure_chains(2) == 0  # a user with nothing connected is left alone


async def test_ensure_chains_keeps_composio_behaviour(cache, fake_bus, state, rec):
    poller = Poller(provider=NativeAware(set()), cache=cache, bus=fake_bus, state=state, schedule=rec.schedule,
                    clock=lambda: NOW)
    await state.update(1, {"polling": {"gmail": True, "slack": False}})
    assert await poller.ensure_chains(1) == 1
    assert [r for _, _, r, _ in rec.scheduled] == ["gmail"]


async def test_the_router_reports_native_use_only_for_an_active_covering_grant(tokens, oauth, client):
    [a] = await make_users(1)
    router = NativeRouter(FakeProvider(), tokens, oauth, [SimpleNamespace(provider=S), SimpleNamespace(provider=G)],
                          client)
    assert not await router.uses_native(a.id, Capability.SLACK)
    await grant_slack(tokens, a.id)
    await grant_google(tokens, a.id, "me@orbit.test")
    assert await router.uses_native(a.id, Capability.SLACK)
    assert await router.uses_native(a.id, Capability.GMAIL) and await router.uses_native(a.id, Capability.CALENDAR)
    assert not await router.uses_native(a.id, Capability.DRIVE)  # drive.readonly was not granted here
    await tokens.mark(a.id, S, REVOKED)
    assert not await router.uses_native(a.id, Capability.SLACK)


# --- 3. identity ------------------------------------------------------------------------------------------


async def test_the_users_own_slack_messages_are_the_user_before_and_after_connect(db):
    [a] = await make_users(1)
    captured = []

    async def sink(job):
        captured.append(job)
        return True

    ing = ConnectorIngest(sink)
    own = {"channel": "D0DM00001", "ts": "1791451800.000100", "user": "U1", "text": "I will send the plan", "team": "T1"}
    await ing.slack(a.id, {**own, "from_me": True})
    await remember_identity(a.id, slack_ids=("U1",), team="T1")
    await ing.slack(a.id, {**own, "ts": "1791451900.000100"})
    assert all(j.people[0].is_user for j in captured) and len(captured) == 2


# --- 4. first sync reads the window through the guard ------------------------------------------------------


async def test_slack_first_sync_backfills_the_setting_window_through_the_ingest_not_the_bus(db, monkeypatch):
    [a] = await make_users(1)
    monkeypatch.setattr("mavis.tools.integrations.first_sync.get_settings",
                        lambda: SimpleNamespace(sync_slack_days=3))
    cid = "C0000GEN01"
    inside, outside = m(-2 * 86400, "fresh message"), m(-5 * 86400, "too old", n=1)
    provider = SlackProvider([c for c in chans() if c["id"] == cid], {cid: [inside, outside]})
    jobs = []

    async def submit(job):
        jobs.append(job)
        return True

    bus = FakeBus()
    sync = FirstSync(provider=provider, memory=SimpleNamespace(learn=_noop), loops=None, bus=bus,
                     tz_of=_tz, clock=lambda: NOW, connectors=ConnectorIngest(submit))
    await sync.run(a.id, Capability.SLACK)
    assert [j.text.rsplit("\n\n", 1)[1] for j in jobs] == ["fresh message"]
    assert not [e for e in bus.events if e.type is EventType.SLACK_MESSAGE]  # history is not news
    oldest = [args["oldest"] for _, act, args in provider.executed if act == "slack.history"]
    assert float(oldest[0]) == pytest.approx(NOW.timestamp() - 3 * 86400, abs=1)


async def _noop(*a, **k):
    return None


async def _tz(_uid):
    return "UTC"


# --- 5. every inbound path reaches the ingest once per record ----------------------------------------------


MAIL = {"messageId": "m77", "threadId": "t77", "sender": "Meera Iyer <meera@vendorco.in>", "subject": "Invoice",
        "messageText": "Invoice 4471 is due on 25 October.", "labelIds": ["INBOX"],
        "messageTimestamp": "2026-10-05T04:25:00Z"}


def test_one_mail_has_one_event_id_whichever_path_saw_it():
    ids = {email_event(1, MAIL, src).id for src in ("poller", "composio", "backfill", "first_sync")}
    assert len(ids) == 1


def test_one_slack_message_has_one_event_id_whichever_path_saw_it():
    raw = {"channel": "C0000GEN01", "ts": "1791451800.000100", "user": "U2", "text": "hello team"}
    ids = {slack_event(1, raw, src).id for src in ("slack", "poller", "backfill", "composio")}
    assert len(ids) == 1
    webhook = slack_events.build_event(1, raw, "C0000GEN01", "slack", from_me=False, team="T1")
    assert webhook.id in ids


async def test_the_bus_passes_a_duplicate_delivery_once_so_the_ingest_runs_once(db):
    [a] = await make_users(1)
    bus, jobs = FakeBus(), []

    async def submit(job):
        jobs.append(job.source_ref)
        return True

    ing = ConnectorIngest(submit)
    for src in ("poller", "composio", "poller"):
        ev = email_event(a.id, MAIL, src)
        if await bus.publish(ev):
            await ing.on_email_event(ev)
    assert jobs == ["gmail:m77"]
    raw = {"channel": "D0DM00001", "ts": "1791451800.000100", "user": "U2", "text": "plan is ready for review"}
    await remember_identity(a.id, team="T1")
    for src in ("slack", "poller", "backfill"):
        ev = slack_events.build_event(a.id, raw, "D0DM00001", src, from_me=False, team="T1")
        if await bus.publish(ev):
            await ing.on_slack_event(ev)
    assert jobs == ["gmail:m77", "slack:T1:D0DM00001:1791451800.000100"]


async def test_slack_reaches_the_ingest_whether_or_not_attention_is_on(db, bus, memory, monkeypatch):
    from mavis.attention import wiring as attention
    from mavis.config import get_settings
    from mavis.worker import runner

    for enabled in ("false", "true"):
        monkeypatch.setenv("ATTENTION_ENABLED", enabled)
        get_settings.cache_clear()
        runner.clear_handlers()
        try:
            wiring.register_integrations()
            attention.register_attention()
            attention.register_attention()
            ingest = attention.get_connector_ingest()
            assert runner._event_handlers[EventType.SLACK_MESSAGE].count(ingest.on_slack_event) == 1
        finally:
            runner.clear_handlers()
            get_settings.cache_clear()
