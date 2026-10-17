from __future__ import annotations

import pytest

from mavis.domain.policy import Capability
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.providers import ProviderSpec


def spec(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco",
                capabilities=(Capability.WEB, Capability.SANDBOX), anchor=Capability.WEB)
    return ProviderSpec(**{**base, **kw})


def test_register_lookup_and_unregister():
    with providers.registered(spec()):
        assert providers.spec_of("testing").display_name == "Testco"
        assert providers.provider_of_capability(Capability.SANDBOX).id == "testing"
        assert providers.family_of(Capability.WEB).id == "testing"
    assert providers.spec_of("testing") is None


def test_rejects_duplicates_long_ids_and_stolen_capabilities():
    with providers.registered(spec()):
        with pytest.raises(ValueError):
            providers.register(spec())
    with pytest.raises(ValueError):
        providers.register(spec(id="x" * 17))
    owner = spec(id="owner", capabilities=(Capability.WEB,), anchor=Capability.WEB)
    with providers.registered(owner):
        try:
            with pytest.raises(ValueError, match="already belong to owner"):
                providers.register(
                    spec(id="other", capabilities=(Capability.WEB,), anchor=Capability.WEB))
        finally:
            providers.unregister("other")


def test_single_capability_is_not_a_family():
    with providers.registered(spec(capabilities=(Capability.WEB,))):
        assert providers.family_of(Capability.WEB) is None
        assert providers.provider_of_capability(Capability.WEB).id == "testing"


def test_family_gate_off_means_no_family():
    with providers.registered(spec(family_enabled=lambda: False)):
        assert providers.family_of(Capability.WEB) is None


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
    with providers.registered(spec(toolkits=frozenset({"testco", "web", "sandbox"}))):
        pass


def test_internal_specs_are_hidden_from_user_providers():
    with providers.registered(spec(internal=True)):
        assert "testing" not in [s.id for s in providers.user_providers()]


def test_rejects_empty_capabilities_and_foreign_anchor():
    with pytest.raises(ValueError, match="at least one capability"):
        providers.register(spec(capabilities=(), anchor=None))
    with pytest.raises(ValueError, match="anchor"):
        providers.register(spec(anchor=Capability.GMAIL))
    assert providers.spec_of("testing") is None


def test_internal_spec_may_have_no_capabilities():
    with providers.registered(spec(internal=True, capabilities=(), anchor=None)):
        assert providers.spec_of("testing") is not None


def test_specs_are_keyword_only():
    with pytest.raises(TypeError):
        ProviderSpec("testing", "Testco", "Testco")  # type: ignore[misc]


def test_caps_for_defaults_to_all_and_honours_hook():
    s = spec()
    assert s.caps_for(object()) == (Capability.WEB, Capability.SANDBOX)
    narrowed = spec(capabilities_for=lambda grant: (Capability.WEB,))
    assert narrowed.caps_for(object()) == (Capability.WEB,)


def test_anchor_capability_falls_back_to_first_when_family_off():
    assert spec(capabilities=(Capability.SANDBOX, Capability.WEB)).anchor_capability() is Capability.WEB
    off = spec(capabilities=(Capability.SANDBOX, Capability.WEB), family_enabled=lambda: False)
    assert off.anchor_capability() is Capability.SANDBOX
    assert spec(anchor=None).anchor_capability() is Capability.WEB


def test_a_poller_for_a_capability_the_spec_does_not_own_is_refused():
    bad = spec(pollers={Capability.CALENDAR: "pkg.mod:fn"})
    with pytest.raises(ValueError, match="poller"):
        providers.register(bad)
    assert providers.spec_of("testing") is None


@pytest.mark.parametrize("path", ["nocolon", "pkg.mod:", ":fn", "pkg mod:fn", "a:b:c", "pkg..mod:fn", ""])
def test_a_malformed_poller_path_is_refused(path):
    with pytest.raises(ValueError, match="module:function"):
        providers.register(spec(pollers={Capability.SANDBOX: path}))
    assert providers.spec_of("testing") is None


def test_a_well_formed_poller_path_is_accepted():
    with providers.registered(spec(pollers={Capability.SANDBOX: "pkg.mod:fn_2"})):
        assert providers.spec_of("testing") is not None
