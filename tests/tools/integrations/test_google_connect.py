"""Connect UX for one Google consent (spec 2026-10-03 section 3.3). Workspace flag on unless stated."""

from __future__ import annotations

from mavis.agents.commands import capability_from_text, run_command
from mavis.domain.events import Event, EventType, JobKind, Trust
from mavis.domain.integrations import ConnectionState
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import GOOGLE_CAPABILITIES
from mavis.tools.integrations.activation import Activator
from mavis.tools.integrations.connect_flow import UPGRADE_TEXT, ConnectFlow
from tests.tools.integrations.fakes import NOW


def make_flow(provider, cache, fake_bus, rec, state, activator=None):
    return ConnectFlow(
        provider=provider, cache=cache, bus=fake_bus, notify=rec.notify, schedule=rec.schedule, state=state,
        base_url="https://mavis.test", clock=lambda: NOW,
        on_active=activator.on_active if activator else None,
        on_google_active=activator.retire_legacy if activator else None,
        on_google_begin=activator.begin_google if activator else None,
    )


def msg(text: str) -> Event:
    return Event(id=f"tg:{text}", user_id=1, type=EventType.USER_MESSAGE, occurred_at=NOW, source="telegram",
                 payload={"text": text}, trust=Trust.USER)


def google_active(provider, user_id: int = 1) -> None:
    for c in GOOGLE_CAPABILITIES:
        provider.set_state(user_id, c, ConnectionState.ACTIVE)


def test_every_google_word_opens_the_one_consent(workspace_on):
    for text in ("google", "gmail", "calendar", "drive", "docs", "sheets", "tasks", "contacts", "meet",
                 "my to-do list"):
        assert capability_from_text(text) is Capability.DRIVE, text
    assert capability_from_text("slack") is Capability.SLACK


def test_flag_off_keeps_gmail_keyword(settings):
    assert capability_from_text("gmail") is Capability.GMAIL
    assert capability_from_text("drive") is None


async def test_connect_google_sends_one_google_link(db, workspace_on, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await run_command(msg("/connect google"), flow)
    assert provider.links[0][1] == "drive"  # the provider turns every Google name into googlesuper
    text = rec.sent[-1].text
    assert "Google" in text and "Google Drive" not in text
    assert rec.sent[-1].buttons[0][0].label == "Connect Google"


async def test_legacy_only_user_still_gets_the_upgrade_link(db, workspace_on, provider, cache, fake_bus, rec,
                                                            state):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await run_command(msg("/connect gmail"), flow)
    assert provider.links and "already connected" not in rec.sent[-1].text


async def test_menu_shows_one_google_workspace_row(db, workspace_on, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.offer_menu(1)
    labels = [row[0].label for row in rec.sent[-1].buttons]
    assert labels == ["Connect Google Workspace", "Connect Slack", "Connect Notion"]


async def test_status_text_explains_legacy_only(db, workspace_on, provider, cache, fake_bus, rec, state):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    text = await flow.status_text(1)
    assert text.splitlines()[0] == ("⚪ Google Workspace: Gmail and Calendar only "
                                    "(send /connect google to add the rest)")


async def test_googlesuper_activation_fans_out_and_retires_legacy(db, workspace_on, provider, cache, fake_bus,
                                                                  rec, state):
    activator = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                          clock=lambda: NOW)
    flow = make_flow(provider, cache, fake_bus, rec, state, activator)
    pid = await flow.start(1, Capability.DRIVE, "")
    google_active(provider)
    await flow.on_connection_changed(Event(
        id="c1", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW, source="integrations",
        payload={"capability": "drive", "state": "ACTIVE", "pending_id": pid},
    ))
    synced = (await state.get(1))["synced"]
    assert set(synced) == {c.value for c in GOOGLE_CAPABILITIES}
    assert {j.payload["capability"] for j in fake_bus.jobs if j.kind is JobKind.FIRST_SYNC} == set(synced)
    subscribed = {t for _, t in provider.subscribed}
    assert {"mail.new_message", "calendar.event_changed", "drive.file_shared", "docs.comment_added",
            "tasks.created", "tasks.updated"} <= subscribed
    assert provider.retired == [1]
    announcements = [m.text for m in rec.sent if m.text.startswith("Connected")]
    assert announcements == ["Connected ✓ I can now work with your Gmail, Calendar, Drive, Docs, Sheets, "
                             "Tasks, Contacts and Meet."]


async def test_legacy_activation_does_not_fan_out(db, workspace_on, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    await flow.reconcile(1, Capability.GMAIL)
    assert set((await state.get(1))["synced"]) == {"gmail"}


async def test_disconnect_google_drops_every_google_capability(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    google_active(provider)
    await state.update(1, {"synced": {c.value: "x" for c in GOOGLE_CAPABILITIES} | {"slack": "x"},
                           "polling": {"gmail": False, "slack": False}})
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await run_command(msg("/disconnect google"), flow)
    assert provider.disconnected == [(1, "drive")]
    st = await state.get(1)
    assert st["synced"] == {"slack": "x"} and st["polling"] == {"slack": False}
    assert rec.sent[-1].text == "Disconnected Google. I can't see it anymore."


async def test_disconnect_gmail_legacy(db, workspace_on, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await run_command(msg("/disconnect gmail-legacy"), flow)
    assert provider.disconnected == [(1, "gmail-legacy")]
    assert rec.sent[-1].text == "Removed the old Gmail connection. Your Google connection is untouched."


async def test_one_reconnect_prompt_for_all_google_capabilities(db, workspace_on, provider, cache, fake_bus,
                                                                rec, state):
    for c in GOOGLE_CAPABILITIES:
        provider.set_state(1, c, ConnectionState.FAILED)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.prompt_reconnect(1, Capability.GMAIL) is True
    assert await flow.prompt_reconnect(1, Capability.DRIVE) is False
    assert await flow.prompt_reconnect(1, Capability.TASKS) is False
    assert (await state.get(1))["reconnect_prompted"] == {"google": NOW.date().isoformat()}


async def test_upgrade_nudge_goes_out_once(db, workspace_on, provider, cache, fake_bus, rec, state):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.maybe_nudge_upgrade(1) is True
    assert await flow.maybe_nudge_upgrade(1) is False
    assert [m.text for m in rec.sent] == [UPGRADE_TEXT]
    assert rec.sent[0].buttons[0][0].data == "conn:start:drive"


async def test_no_nudge_when_flag_off(db, settings, provider, cache, fake_bus, rec, state):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.maybe_nudge_upgrade(1) is False
    assert rec.sent == []


async def test_no_nudge_once_upgraded(db, workspace_on, provider, cache, fake_bus, rec, state):
    google_active(provider)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.maybe_nudge_upgrade(1) is False


async def _activate_google(provider, cache, fake_bus, rec, state):
    activator = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                          clock=lambda: NOW)
    flow = make_flow(provider, cache, fake_bus, rec, state, activator)
    pid = await flow.start(1, Capability.DRIVE, "")
    google_active(provider)
    await flow.on_connection_changed(Event(
        id="c1", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW, source="integrations",
        payload={"capability": "drive", "state": "ACTIVE", "pending_id": pid},
    ))


async def test_legacy_triggers_retire_only_after_googlesuper_subscribed(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    calls: list[str] = []
    real_subscribe, real_retire = provider.subscribe, provider.retire_legacy_triggers

    async def subscribe(user, trigger, config):
        calls.append("subscribe")
        return await real_subscribe(user, trigger, config)

    async def retire(user):
        calls.append("retire")
        return await real_retire(user)

    provider.subscribe, provider.retire_legacy_triggers = subscribe, retire
    await _activate_google(provider, cache, fake_bus, rec, state)
    assert calls.count("retire") == 1
    assert calls.index("retire") == len(calls) - 1  # after every subscribe
    assert "subscribe" in calls


async def test_failed_subscribe_keeps_legacy_triggers(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    provider.fail_subscribe = True
    await _activate_google(provider, cache, fake_bus, rec, state)
    assert provider.retired == []


async def test_attention_off_subscribes_no_workspace_triggers(
    db, workspace_on, monkeypatch, provider, cache, fake_bus, rec, state
):
    from mavis.config import get_settings

    monkeypatch.setenv("ATTENTION_ENABLED", "false")
    get_settings.cache_clear()
    await _activate_google(provider, cache, fake_bus, rec, state)
    subscribed = {t for _, t in provider.subscribed}
    assert subscribed == {"mail.new_message", "calendar.event_changed"}
    assert provider.retired == [1]


async def test_stale_legacy_subscribe_failure_does_not_block_retirement(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    activator = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                          clock=lambda: NOW)
    provider.fail_subscribe = True
    await activator.on_active(1, Capability.GMAIL)  # legacy activation, no fan-out, fails
    provider.fail_subscribe = False
    await _activate_google(provider, cache, fake_bus, rec, state)
    assert provider.retired == [1]


async def test_second_google_reconnect_prompt_after_reactivation_same_day(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    google_active(provider)
    await state.update(1, {"reconnect_prompted": {"google": NOW.date().isoformat()}})
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.reconcile(1, Capability.GMAIL)
    assert "google" not in (await state.get(1)).get("reconnect_prompted", {})


async def test_disconnect_legacy_other_error_is_not_reported_as_missing(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    from mavis.domain.errors import IntegrationError

    async def boom(user, toolkit):
        raise IntegrationError("Composio answered 500")

    provider.disconnect = boom
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.disconnect_legacy(1, "gmail-legacy")
    assert "no old" not in rec.sent[-1].text and "couldn't remove" in rec.sent[-1].text


async def test_repeated_connection_events_fan_out_and_retire_once(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    await _activate_google(provider, cache, fake_bus, rec, state)
    activator = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=False,
                          clock=lambda: NOW)
    flow = make_flow(provider, cache, fake_bus, rec, state, activator)
    for i in range(2):
        await flow.on_connection_changed(Event(
            id=f"again{i}", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW,
            source="integrations", payload={"capability": "drive", "state": "ACTIVE", "pending_id": 0},
        ))
    assert provider.retired == [1]


def _legacy_only_disconnect(provider, legacy: set[str]):
    """The provider has no googlesuper row: a Google disconnect raises NoSuchConnection; legacy aliases
    in `legacy` exist and are removed."""
    from mavis.domain.errors import NoSuchConnection

    async def disconnect(user, toolkit):
        if toolkit in legacy:
            legacy.discard(toolkit)
            provider.disconnected.append((user.user_id, toolkit))
            return
        raise NoSuchConnection(f"there is no {toolkit} connection to remove.")

    provider.disconnect = disconnect


async def test_disconnect_google_legacy_only_removes_the_legacy_accounts(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.set_state(1, Capability.CALENDAR, ConnectionState.ACTIVE)
    await state.update(1, {"synced": {"gmail": "x", "googlecalendar": "x", "slack": "x"}})
    _legacy_only_disconnect(provider, {"gmail-legacy", "calendar-legacy"})
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await run_command(msg("/disconnect gmail"), flow)
    assert sorted(t for _, t in provider.disconnected) == ["calendar-legacy", "gmail-legacy"]
    assert (await state.get(1))["synced"] == {"slack": "x"}
    assert rec.sent[-1].text == "Disconnected Google. I can't see it anymore."


async def test_disconnect_google_with_no_connection_at_all_says_so(
    db, workspace_on, provider, cache, fake_bus, rec, state
):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)  # cache says Gmail; provider has none
    _legacy_only_disconnect(provider, set())
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await run_command(msg("/disconnect gmail"), flow)
    assert rec.sent[-1].text == "There's no Google connection to remove."


def test_slack_and_notion_win_over_google_words(workspace_on):
    assert capability_from_text("notion docs") is Capability.NOTION
    assert capability_from_text("slack tasks") is Capability.SLACK
    assert capability_from_text("my notion to-do list") is Capability.NOTION
    assert capability_from_text("google docs") is Capability.DRIVE
