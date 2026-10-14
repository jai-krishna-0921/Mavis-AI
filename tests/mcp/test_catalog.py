"""The static catalog of hosted MCP servers."""

from __future__ import annotations

import re

import pytest

from mavis.connectors import netguard
from mavis.mcp import catalog, slugs

DASHES = re.compile("[–—]")


def test_the_catalog_has_at_most_twelve_entries_with_unique_ids() -> None:
    ids = [e.id for e in catalog.CATALOG]
    assert 1 <= len(ids) <= 12 and len(set(ids)) == len(ids)


@pytest.mark.parametrize("entry", catalog.CATALOG, ids=lambda e: e.id)
def test_every_entry_is_well_formed(settings, entry) -> None:
    assert slugs.is_slug(entry.id) and slugs.slugify(entry.name)
    assert entry.auth in ("none", "oauth")
    assert netguard.validate_url(entry.url).scheme == "https"  # https, a name, no credentials
    assert "?" not in entry.url and "#" not in entry.url
    assert "\n" not in entry.description and 10 <= len(entry.description) <= 100
    assert not DASHES.search(entry.name + entry.description)
    assert entry.client_id is None and entry.allowed_hosts == ()


def test_the_catalog_needs_no_client_secret_anywhere() -> None:
    assert not any(hasattr(e, "client_secret") for e in catalog.CATALOG)
    assert {e.auth for e in catalog.CATALOG} == {"none", "oauth"}


def test_lookup_and_the_api_shape() -> None:
    assert catalog.get("linear").url == "https://mcp.linear.app/mcp"
    assert catalog.get("nope") is None
    shown = catalog.as_dicts()
    assert {"id", "name", "description", "auth", "url"} == set(shown[0])
    assert len(shown) == len(catalog.CATALOG)


def test_the_extra_hosts_come_from_the_matching_entry_only() -> None:
    assert catalog.allowed_hosts_for_url("https://mcp.linear.app/mcp") == ()
    assert catalog.allowed_hosts_for_url("https://custom.example.com/mcp") == ()
