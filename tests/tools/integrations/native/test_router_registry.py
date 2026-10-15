from __future__ import annotations

from datetime import timedelta

from mavis.domain import timeutil
from mavis.domain.integrations import ConnectionState, ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.providers import CatalogEntry, ProviderSpec
from mavis.tools.integrations.native.router import (
    NativeRouter,
    allows,
    covers,
    feature_access,
    google_features,
    provider_of,
)
from tests.tools.integrations.fakes import FakeProvider

T = NativeProvider.TESTING
U = UserRef(user_id=1)
NEED = frozenset({"mail.read"})


class Exec:
    provider = T

    def __init__(self):
        self.calls = []

    def handles(self, action):
        return action == "notion.search"

    async def execute(self, user, action, args):
        self.calls.append(action)
        return ToolResult(ok=True, data={"native": action})


def fake(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco", auth_url="https://idp.test/a",
                token_url="https://idp.test/t", client_id_setting="slack_client_id",
                client_secret_setting="slack_client_secret",
                capabilities=(Capability.NOTION, Capability.SANDBOX), anchor=Capability.NOTION,
                toolkits=frozenset({"testco", "notion", "sandbox"}), account_key=lambda a: a.get("k"),
                catalog=CatalogEntry(description="d", services={}, icon="x"))
    return ProviderSpec(**{**base, **kw})


async def grant(tokens, scopes=("mail.read",), plan="work", status="ACTIVE", user=1):
    await tokens.save(user, T, account={"scopes": list(scopes), "k": f"k{user}", "plan": plan},
                      access_token="a", refresh_token="r", expires_at=timeutil.now() + timedelta(hours=1))
    if status != "ACTIVE":
        await tokens.mark(user, T, status)
    return await tokens.grant(user, T)


def test_provider_of_follows_the_table(isolated_registry):
    assert provider_of(Capability.NOTION) is None
    isolated_registry.register(fake())
    assert provider_of(Capability.NOTION) is T and provider_of(Capability.SANDBOX) is T
    assert provider_of(Capability.GMAIL) is NativeProvider.GOOGLE
    assert provider_of(Capability.SLACK) is NativeProvider.SLACK


async def test_scope_table_decides_allows_and_covers(tokens, isolated_registry):
    isolated_registry.register(fake(action_scopes={"notion.search": (NEED,)}))
    ok = await grant(tokens)
    assert allows(ok, "notion.search") and covers(ok, Capability.NOTION)
    assert not covers(ok, Capability.SANDBOX)  # no action of that capability is allowed
    await tokens.save(1, T, account={"scopes": ["other"], "k": "k1"}, access_token="a", refresh_token="r",
                      expires_at=timeutil.now() + timedelta(hours=1))
    bare = await tokens.grant(1, T)
    assert not allows(bare, "notion.search") and not covers(bare, Capability.NOTION)


async def test_empty_scope_table_means_any_active_grant_covers(tokens, isolated_registry):
    isolated_registry.register(fake())
    g = await grant(tokens, scopes=())
    assert covers(g, Capability.NOTION) and covers(g, Capability.SANDBOX) and allows(g, "notion.search")


async def test_capabilities_for_trims_what_a_grant_serves(tokens, isolated_registry):
    isolated_registry.register(fake(
        capabilities_for=lambda g: (Capability.NOTION,) if g.account.get("plan") == "personal"
        else (Capability.NOTION, Capability.SANDBOX)))
    personal = await grant(tokens, plan="personal")
    assert covers(personal, Capability.NOTION) and not covers(personal, Capability.SANDBOX)


async def test_status_marks_every_capability_of_the_family(tokens, oauth, client, isolated_registry):
    isolated_registry.register(fake(
        capabilities_for=lambda g: (Capability.NOTION,) if g.account.get("plan") == "personal"
        else (Capability.NOTION, Capability.SANDBOX)))
    router = NativeRouter(FakeProvider(), tokens, oauth, [Exec()], client)
    await grant(tokens)
    states = await router.status(U)
    assert states["notion"] is ConnectionState.ACTIVE and states["sandbox"] is ConnectionState.ACTIVE
    await tokens.mark(1, T, "REVOKED")
    states = await router.status(U)
    assert states["notion"] is ConnectionState.FAILED and states["sandbox"] is ConnectionState.FAILED


async def test_execute_routes_through_the_spec_executor(tokens, oauth, client, isolated_registry):
    isolated_registry.register(fake(action_scopes={"notion.search": (NEED,)}))
    ex, fallback = Exec(), FakeProvider()
    router = NativeRouter(fallback, tokens, oauth, [ex], client)
    await grant(tokens)
    assert (await router.execute(U, "notion.search", {})).data == {"native": "notion.search"}
    assert fallback.executed == [] and await router.uses_native(1, Capability.NOTION)
    await grant(tokens, scopes=("other",))
    res = await router.execute(U, "notion.search", {})
    assert res.ok is False and "reconnect" in res.error


async def test_toolkit_names_come_from_the_spec(tokens, oauth, client, isolated_registry):
    isolated_registry.register(fake())
    router = NativeRouter(FakeProvider(), tokens, oauth, [Exec()], client)
    url = await router.connect_link(U, "Testco", "https://mavis.test/connect/callback?p=3")
    assert url.startswith("https://idp.test/a?")
    assert router.can_connect(Capability.NOTION) is True


async def test_feature_wording_comes_from_the_spec(tokens, oauth, client, isolated_registry):
    isolated_registry.register(fake(action_scopes={"notion.search": (NEED,)},
                                    features=(("search pages", "notion.search"),
                                              ("edit pages", "notion.create_page"))))
    router = NativeRouter(FakeProvider(), tokens, oauth, [Exec()], client)
    g = await grant(tokens)
    assert feature_access(g) == (["search pages"], ["edit pages"])
    assert await router.provider_access(1, T) == (["search pages"], ["edit pages"])
    assert google_features is not None


def test_google_and_slack_behaviour_is_unchanged():
    from mavis.tools.integrations.actions import GOOGLE_CAPABILITIES
    from mavis.tools.integrations.native import providers

    assert set(providers.spec_of("google").capabilities) == set(GOOGLE_CAPABILITIES)
    assert providers.spec_of("google").toolkits >= {"google", "googlesuper", "gmail", "googlecalendar"}
