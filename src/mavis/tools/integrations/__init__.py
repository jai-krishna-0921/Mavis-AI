"""Integration provider factory. Swap providers via INTEGRATION_PROVIDER without touching agents."""

from __future__ import annotations

from functools import lru_cache

from mavis.config import get_settings
from mavis.domain.errors import IntegrationError
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache


@lru_cache
def get_provider() -> IntegrationProvider:
    s = get_settings()
    if s.integration_provider == "composio":
        from mavis.tools.integrations.composio import ComposioProvider

        return ComposioProvider(
            api_key=s.composio_api_key, base_url=s.composio_base_url,
            webhook_secret=s.composio_webhook_secret, timeout_s=s.composio_timeout_s,
        )
    raise IntegrationError(f"unknown INTEGRATION_PROVIDER {s.integration_provider!r}")


@lru_cache
def get_connection_cache() -> ConnectionCache:
    return ConnectionCache(get_provider(), ttl_s=get_settings().integration_status_ttl_s)


async def close_provider() -> None:
    """Close the provider's HTTP client if one was created in this process (never builds one)."""
    if get_provider.cache_info().currsize == 0:
        return
    aclose = getattr(get_provider(), "aclose", None)
    if aclose is not None:
        await aclose()
