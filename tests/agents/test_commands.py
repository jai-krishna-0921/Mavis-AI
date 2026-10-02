from mavis.agents.commands import (
    NOT_CONFIGURED_TEXT,
    capability_from_text,
    handle_connect,
    parse_command,
    run_command,
)
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.integrations import ConnectionState
from mavis.domain.policy import Capability
from mavis.tools.integrations.connect_flow import ConnectFlow
from tests.tools.integrations.fakes import NOW


def msg(text):
    return Event(id=f"tg:{text}", user_id=1, type=EventType.USER_MESSAGE, occurred_at=NOW, source="telegram",
                 payload={"text": text}, trust=Trust.USER)


def flow_for(provider, cache, fake_bus, rec, state):
    return ConnectFlow(provider=provider, cache=cache, bus=fake_bus, notify=rec.notify, schedule=rec.schedule,
                       state=state, base_url="https://mavis.test", clock=lambda: NOW)


def test_parse_command():
    assert parse_command("/connect gmail") == ("connect", ["gmail"])
    assert parse_command("/Connect@Mavis247_bot   Slack") == ("connect", ["Slack"])
    assert parse_command("connect gmail") is None


def test_capability_from_text():
    assert capability_from_text("hook up my inbox") is Capability.GMAIL
    assert capability_from_text("connect google calendar") is Capability.CALENDAR
    assert capability_from_text("Slack please") is Capability.SLACK
    assert capability_from_text("my notion") is Capability.NOTION
    assert capability_from_text("something else") is None


async def test_connect_command_starts_flow(db, provider, cache, fake_bus, rec, state):
    flow = flow_for(provider, cache, fake_bus, rec, state)
    assert await run_command(msg("/connect gmail"), flow) is True
    assert rec.sent[-1].buttons[0][0].url == "https://connect.example/gmail"


async def test_connect_without_arg_offers_menu(db, provider, cache, fake_bus, rec, state):
    flow = flow_for(provider, cache, fake_bus, rec, state)
    await run_command(msg("/connect"), flow)
    assert rec.sent[-1].text == "Which one should I hook up?"


async def test_connections_and_disconnect(db, provider, cache, fake_bus, rec, state):
    provider.set_state(1, Capability.SLACK, ConnectionState.ACTIVE)
    flow = flow_for(provider, cache, fake_bus, rec, state)
    await run_command(msg("/connections"), flow)
    assert "✅ Slack" in rec.sent[-1].text
    await run_command(msg("/disconnect slack"), flow)
    assert provider.disconnected == [(1, "slack")]
    await run_command(msg("/disconnect"), flow)
    assert rec.sent[-1].text.startswith("Which one?")


async def test_unknown_command_and_plain_text_not_handled(db, provider, cache, fake_bus, rec, state):
    flow = flow_for(provider, cache, fake_bus, rec, state)
    assert await run_command(msg("/weather"), flow) is False
    assert await run_command(msg("hi"), flow) is False


async def test_handle_connect_from_natural_language(db, provider, cache, fake_bus, rec, state):
    flow = flow_for(provider, cache, fake_bus, rec, state)
    assert await handle_connect(1, "can you connect my calendar?", flow) is None
    assert provider.links[-1][1] == "googlecalendar"
    await handle_connect(1, "connect stuff", flow)
    assert rec.sent[-1].text == "Which one should I hook up?"


async def test_unconfigured_provider_gets_friendly_message(db, provider, cache, fake_bus, rec, state):
    provider.configured = False
    flow = flow_for(provider, cache, fake_bus, rec, state)
    for text in ("/connect gmail", "/connections", "/disconnect gmail"):
        assert await run_command(msg(text), flow) is True
        assert rec.sent[-1].text == NOT_CONFIGURED_TEXT
    await handle_connect(1, "connect my inbox", flow)
    assert rec.sent[-1].text == NOT_CONFIGURED_TEXT
    assert provider.links == [] and provider.disconnected == []
    assert "—" not in NOT_CONFIGURED_TEXT and "–" not in NOT_CONFIGURED_TEXT
