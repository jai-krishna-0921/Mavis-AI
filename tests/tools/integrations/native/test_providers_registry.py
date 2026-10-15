from __future__ import annotations

import pytest

from mavis.domain.policy import Capability
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.providers import ProviderSpec


def spec(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco",
                capabilities=(Capability.NOTION, Capability.SANDBOX), anchor=Capability.NOTION)
    return ProviderSpec(**{**base, **kw})


def test_register_lookup_and_unregister():
    with providers.registered(spec()):
        assert providers.spec_of("testing").display_name == "Testco"
        assert providers.provider_of_capability(Capability.SANDBOX).id == "testing"
        assert providers.family_of(Capability.NOTION).id == "testing"
    assert providers.spec_of("testing") is None


def test_rejects_duplicates_long_ids_and_stolen_capabilities():
    with providers.registered(spec()):
        with pytest.raises(ValueError):
            providers.register(spec())
    with pytest.raises(ValueError):
        providers.register(spec(id="x" * 17))
    owner = spec(id="owner", capabilities=(Capability.GMAIL,), anchor=Capability.GMAIL)
    with providers.registered(owner), pytest.raises(ValueError):
        providers.register(spec(id="other", capabilities=(Capability.GMAIL,), anchor=Capability.GMAIL))


def test_single_capability_is_not_a_family():
    with providers.registered(spec(capabilities=(Capability.NOTION,))):
        assert providers.family_of(Capability.NOTION) is None
        assert providers.provider_of_capability(Capability.NOTION).id == "testing"


def test_family_gate_off_means_no_family():
    with providers.registered(spec(family_enabled=lambda: False)):
        assert providers.family_of(Capability.NOTION) is None


def test_configured_needs_kek_and_both_credentials(monkeypatch, native_env):
    s = spec(client_id_setting="slack_client_id", client_secret_setting="slack_client_secret")
    assert providers.configured(s) is True
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "")
    from mavis.config import get_settings
    get_settings.cache_clear()
    assert providers.configured(s) is False


def test_toolkits_must_cover_every_capability_value():
    with pytest.raises(ValueError):
        providers.register(spec(toolkits=frozenset({"testco"})))
    with providers.registered(spec(toolkits=frozenset({"testco", "notion", "sandbox"}))):
        pass


def test_internal_specs_are_hidden_from_user_providers():
    with providers.registered(spec(internal=True)):
        assert "testing" not in [s.id for s in providers.user_providers()]
