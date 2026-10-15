"""Account deletion and memory sources read the provider table: a vendor added later cannot be forgotten."""

from __future__ import annotations

import functools
from datetime import timedelta

from mavis.access import deletion
from mavis.domain import timeutil
from mavis.domain.policy import Capability
from mavis.memory import controls, vault
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.providers import ProviderSpec
from mavis.tools.integrations.native.router import NativeRouter
from tests.tools.integrations.fakes import FakeProvider
from tests.tools.integrations.native import conftest as native_conftest

# the native suite's fixtures
isolated_registry = native_conftest.isolated_registry
native_env = native_conftest.native_env
vendor = native_conftest.vendor
client = native_conftest.client
tokens = native_conftest.tokens
oauth = native_conftest.oauth

T = NativeProvider.TESTING
revoked: list[str] = []


async def _revoke(req):
    revoked.append(req.access_token)


def fake() -> ProviderSpec:
    return ProviderSpec(
        id="testing", display_name="Testco", brand="Testco", revoke=_revoke,
        capabilities=(Capability.NOTION,), anchor=Capability.NOTION,
        account_key=lambda a: a.get("k"),
        memory_sources={"tco": ("tco:",), "tcal": ("tcal:", "tco-cal:")},
    )


def test_memory_sources_extend_vault_and_controls(isolated_registry):
    isolated_registry.register(fake())
    assert vault.connector_prefixes()["tcal"] == ("tcal:", "tco-cal:")
    assert "tco" in controls.connectors()
    assert {"gmail", "slack", "calendar"} <= set(controls.connectors())


def test_builtin_memory_sources_unchanged():
    assert vault.connector_prefixes() == {
        "gmail": ("gmail:",), "slack": ("slack:",), "calendar": ("calendar:",)}
    assert set(controls.connectors()) == {"gmail", "slack", "calendar"}


async def test_account_deletion_revokes_every_registered_provider(
        tokens, oauth, client, isolated_registry, monkeypatch):
    isolated_registry.register(fake())
    revoked.clear()
    soon = timeutil.now() + timedelta(hours=1)
    for provider, name in ((NativeProvider.GOOGLE, "g"), (NativeProvider.SLACK, "s"), (T, "t")):
        await tokens.save(1, provider, account={"k": f"k-{name}", "scopes": [], "team_id": "T1",
                                                 "email": "a@b.c"},
                          access_token=f"tok-{name}", refresh_token=None, expires_at=soon)
    await tokens.save(1, NativeProvider.SLACK_BOT, account={"team_id": "T1", "scopes": []},
                      access_token="bot", refresh_token=None, expires_at=None)
    router = NativeRouter(FakeProvider(), tokens, oauth, [], client)
    monkeypatch.setattr("mavis.tools.integrations.get_provider", functools.cache(lambda: router))
    out = await deletion._native(1)
    assert "tok-t" in revoked
    assert out["revoked"] == 3
    for provider in NativeProvider:
        assert await tokens.grant(1, provider) is None


def test_vault_connector_matches_by_registered_prefix_not_by_name(isolated_registry):
    isolated_registry.register(ProviderSpec(
        id="testing", display_name="Testco", brand="Testco", capabilities=(Capability.NOTION,),
        anchor=Capability.NOTION, memory_sources={"outlook_calendar": ("ocal:",), "outlook": ("ocal:mail:",)}))
    assert vault._connector(["ocal:abc"]) == "outlook_calendar"
    assert vault._connector(["ocal:mail:abc"]) == "outlook"  # longest prefix wins
    assert vault._connector(["you", "gmail:thread"]) == "gmail"
    assert vault._connector(["outlook_calendar:x", "you"]) == ""  # the name is not the prefix
