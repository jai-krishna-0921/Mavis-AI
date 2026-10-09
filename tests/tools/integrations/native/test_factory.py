import pytest

from mavis.config import get_settings
from mavis.tools.integrations import get_provider
from mavis.tools.integrations.composio import ComposioProvider
from mavis.tools.integrations.native.router import NativeRouter


@pytest.fixture(autouse=True)
def _fresh_provider():
    get_provider.cache_clear()
    yield
    get_provider.cache_clear()


def test_composio_stays_the_default(settings):
    assert isinstance(get_provider(), ComposioProvider)


async def test_native_builds_the_router_over_composio(settings, monkeypatch):
    monkeypatch.setenv("INTEGRATION_PROVIDER", "native")
    get_settings.cache_clear()
    provider = get_provider()
    try:
        assert isinstance(provider, NativeRouter) and isinstance(provider.fallback, ComposioProvider)
    finally:
        await provider.aclose()


def test_new_settings_defaults(settings):
    s = get_settings()
    assert (s.sync_gmail_days, s.sync_slack_days, s.sync_calendar_back_days, s.sync_calendar_ahead_days) == (
        14, 7, 7, 30)
    assert s.native_token_kek == "" and s.slack_signing_secret == ""
