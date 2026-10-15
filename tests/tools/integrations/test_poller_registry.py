# tests/tools/integrations/test_poller_registry.py
from __future__ import annotations

import importlib

import pytest

from mavis.domain.integrations import ConnectionState
from mavis.domain.policy import Capability
from mavis.tools.integrations import poller as poller_mod
from mavis.tools.integrations.activation import Activator
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.providers import PollAuthError, ProviderSpec
from mavis.tools.integrations.poller import POLL_INTERVAL, POLL_KIND, Poller, hook_for, pollable
from tests.tools.integrations.fakes import NOW

SB = Capability.SANDBOX


@pytest.fixture
def isolated_registry():
    """Snapshot PROVIDERS so a test may register fakes without leaking (the native suite has its own)."""
    saved = dict(providers.PROVIDERS)
    yield providers
    providers.PROVIDERS.clear()
    providers.PROVIDERS.update(saved)

seen: list[tuple[int, dict]] = []
mode = {"auth": False}


async def hook(poller, user_id, st):
    seen.append((user_id, dict(st.get("cursors", {}))))
    if mode["auth"]:
        raise PollAuthError
    await poller.set_cursor(user_id, st, "testing_cursor", "c1")
    return 3


def fake(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco", capabilities=(Capability.NOTION, SB),
                anchor=Capability.NOTION,
                pollers={SB: "tests.tools.integrations.test_poller_registry:hook"})
    return ProviderSpec(**{**base, **kw})


def make(provider, cache, fake_bus, state, rec, **kw):
    return Poller(provider=provider, cache=cache, bus=fake_bus, state=state, schedule=rec.schedule,
                  clock=lambda: NOW, **kw)


def test_builtin_pollable_set_is_unchanged():
    assert pollable() == frozenset({Capability.GMAIL, Capability.CALENDAR, Capability.SLACK})
    assert not hasattr(poller_mod, "POLLABLE")


def test_registered_hook_is_pollable_and_resolved(isolated_registry):
    isolated_registry.register(fake())
    assert SB in pollable() and Capability.NOTION not in pollable()
    assert hook_for(SB) is hook
    assert hook_for(Capability.NOTION) is None


async def test_poll_runs_the_provider_hook_and_reschedules(db, isolated_registry, provider, cache, fake_bus,
                                                           state, rec):
    isolated_registry.register(fake())
    seen.clear()
    mode["auth"] = False
    provider.set_state(1, SB, ConnectionState.ACTIVE)
    await state.update(1, {"polling": {"sandbox": True}})
    assert await make(provider, cache, fake_bus, state, rec).poll(1, SB) == 3
    assert (await state.get(1))["cursors"] == {"testing_cursor": "c1"}
    assert rec.scheduled == [(1, NOW + POLL_INTERVAL, "sandbox", POLL_KIND)]


async def test_hook_auth_error_stops_the_chain_only_on_failed(
    db, isolated_registry, provider, cache, fake_bus, state, rec,
):
    isolated_registry.register(fake())
    mode["auth"] = True
    provider.set_state(1, SB, ConnectionState.ACTIVE)
    await state.update(1, {"polling": {"sandbox": True}})
    p = make(provider, cache, fake_bus, state, rec)
    assert await p.poll(1, SB) == 0
    assert rec.scheduled  # provider still says ACTIVE: keep polling
    rec.scheduled.clear()
    provider.set_state(1, SB, ConnectionState.FAILED)
    cache.invalidate(1)
    assert await p.poll(1, SB) == 0
    assert rec.scheduled == [] and "sandbox" not in (await state.get(1))["polling"]
    mode["auth"] = False


async def test_activation_polls_a_registered_capability(db, isolated_registry, provider, state, rec):
    isolated_registry.register(fake())
    act = Activator(provider=provider, state=state, schedule=rec.schedule, polling_forced=True,
                    clock=lambda: NOW)
    assert await act.on_active(1, SB) is True
    assert rec.scheduled == [(1, NOW, "sandbox", POLL_KIND)]
    assert await act.on_active(1, Capability.NOTION) is False  # no hook: never polled


async def test_ensure_chains_arms_registered_capabilities(db, isolated_registry, provider, cache, fake_bus,
                                                          state, rec):
    isolated_registry.register(fake())
    await state.update(1, {"polling": {"sandbox": True}})
    assert await make(provider, cache, fake_bus, state, rec).ensure_chains(1) == 1


def test_every_registered_poller_path_resolves_to_a_callable():
    paths = [(s.id, c, p) for s in providers.PROVIDERS.values() for c, p in s.pollers.items()]
    assert {c for _, c, _ in paths} == {Capability.GMAIL, Capability.CALENDAR, Capability.SLACK}
    for _, capability, path in paths:
        module, attr = path.split(":", 1)
        resolved = getattr(importlib.import_module(module), attr)
        assert callable(resolved), path
        assert hook_for(capability) is resolved
    assert hook_for(Capability.GMAIL) is poller_mod.poll_gmail
    assert hook_for(Capability.CALENDAR) is poller_mod.poll_calendar
    assert hook_for(Capability.SLACK) is poller_mod.poll_slack
