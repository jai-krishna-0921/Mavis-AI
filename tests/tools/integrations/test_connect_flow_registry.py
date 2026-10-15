"""ConnectFlow with a registered fake family (NOTION + SANDBOX): fan-out, keys, announcement, forget."""

from __future__ import annotations

from mavis.domain.events import Event, EventType
from mavis.domain.integrations import ConnectionState
from mavis.domain.policy import Capability
from mavis.tools.integrations.connect_flow import ConnectFlow
from mavis.tools.integrations.native.providers import ProviderSpec
from tests.tools.integrations.fakes import NOW
from tests.tools.integrations.native import conftest as native_conftest

isolated_registry = native_conftest.isolated_registry  # the native suite's registry snapshot fixture

N, SB = Capability.NOTION, Capability.SANDBOX


def fake(**kw) -> ProviderSpec:
    base = dict(
        id="testing",
        display_name="Testco",
        brand="Testco",
        capabilities=(N, SB),
        anchor=N,
        abilities="notes and boxes",
        reconnect_hint="send /connect testing and allow everything",
        forget_prefixes=("tco:", "tbox:"),
        family_label="Testco Suite",
    )
    return ProviderSpec(**{**base, **kw})


def make_flow(provider, cache, fake_bus, rec, state, forget=None, **kw):
    return ConnectFlow(
        provider=provider,
        cache=cache,
        bus=fake_bus,
        notify=rec.notify,
        schedule=rec.schedule,
        state=state,
        base_url="https://mavis.test",
        clock=lambda: NOW,
        forget_source=forget,
        **kw,
    )


def changed(pid):
    return Event(
        id=f"c{pid}",
        user_id=1,
        type=EventType.CONNECTION_CHANGED,
        occurred_at=NOW,
        source="integrations",
        payload={"capability": "notion", "state": "ACTIVE", "pending_id": pid},
    )


async def test_state_is_active_when_any_member_is(isolated_registry):
    isolated_registry.register(fake())
    states = {"sandbox": ConnectionState.ACTIVE}
    assert ConnectFlow._state_of(N, states) is ConnectionState.ACTIVE
    assert ConnectFlow._state_of(N, {}) is ConnectionState.NONE
    failed = {"slack": ConnectionState.FAILED}
    assert ConnectFlow._state_of(Capability.SLACK, failed) is ConnectionState.FAILED


async def test_one_consent_activates_every_live_member(
    db, isolated_registry, provider, cache, fake_bus, rec, state
):
    isolated_registry.register(fake())
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, N, "")
    provider.set_state(1, N, ConnectionState.ACTIVE)
    provider.set_state(1, SB, ConnectionState.ACTIVE)
    await flow.on_connection_changed(changed(pid))
    assert set((await state.get(1))["synced"]) == {"notion", "sandbox"}
    assert "notes and boxes" in rec.sent[-1].text


async def test_only_live_members_activate(db, isolated_registry, provider, cache, fake_bus, rec, state):
    isolated_registry.register(fake())
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, N, "")
    provider.set_state(1, N, ConnectionState.ACTIVE)
    provider.set_state(1, SB, ConnectionState.ACTIVE)
    provider.set_state(1, SB, ConnectionState.FAILED)
    await flow.on_connection_changed(changed(pid))
    assert set((await state.get(1))["synced"]) == {"notion"}


async def test_reconnect_prompt_is_once_a_day_per_family(
    db, isolated_registry, provider, cache, fake_bus, rec, state
):
    isolated_registry.register(fake())
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.prompt_reconnect(1, N) is True
    assert await flow.prompt_reconnect(1, SB) is False  # same family key
    assert "testing" in (await state.get(1))["reconnect_prompted"]


async def test_menu_collapses_the_family_into_one_row(
    db, isolated_registry, provider, cache, fake_bus, rec, state
):
    isolated_registry.register(fake())
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.offer_menu(1)
    labels = [row[0].label for row in rec.sent[-1].buttons]
    assert "Connect Testco Suite" in labels and "Connect Notion" not in labels


async def test_disconnect_forgets_every_spec_prefix(
    db, isolated_registry, provider, cache, fake_bus, rec, state
):
    isolated_registry.register(fake())
    calls = []

    async def forget(user_id, prefix):
        calls.append(prefix)
        return 2

    flow = make_flow(provider, cache, fake_bus, rec, state, forget=forget)
    provider.set_state(1, N, ConnectionState.ACTIVE)
    assert await flow.disconnect(1, N) is True
    assert calls == ["tco:", "tbox:"] and "removed what I had learned" in rec.sent[-1].text
    dropped = (await state.get(1)).get("synced", {})
    assert "notion" not in dropped and "sandbox" not in dropped


async def test_hidden_family_behaves_like_single_capabilities(
    db, isolated_registry, provider, cache, fake_bus, rec, state
):
    isolated_registry.register(fake(family_enabled=lambda: False))
    assert ConnectFlow._state_of(N, {"sandbox": ConnectionState.ACTIVE}) is ConnectionState.NONE


async def test_calendar_like_member_alone_with_family_off_forgets_nothing(
    db, isolated_registry, provider, cache, fake_bus, rec, state
):
    isolated_registry.register(fake(family_enabled=lambda: False))
    calls = []

    async def forget(user_id, prefix):
        calls.append(prefix)
        return 1

    flow = make_flow(provider, cache, fake_bus, rec, state, forget=forget)
    provider.set_state(1, SB, ConnectionState.ACTIVE)
    assert await flow.disconnect(1, SB) is True
    assert calls == []


async def test_family_announcement_lists_what_was_refused(
    db, isolated_registry, provider, cache, fake_bus, rec, state
):
    isolated_registry.register(fake())

    async def access(user_id, spec_id):
        return (["read notes"], ["write boxes"])

    provider.provider_access = access
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow._announce(1, N)
    assert rec.sent[-1].text == (
        "Connected ✓ I can read notes. Not allowed on Testco's screen: write boxes. "
        "To add them, send /connect testing and allow everything."
    )
