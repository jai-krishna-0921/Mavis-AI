"""Composio provider calls resolve the provider-side user id from the users table (Phase 11), so every test
in this package runs against a fresh schema. A user with no row keeps the legacy `mavis-<id>`."""

import pytest


@pytest.fixture(autouse=True)
def _schema(db):
    yield
