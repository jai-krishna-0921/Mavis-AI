from __future__ import annotations

import pytest

from tests.tools.integrations.native.notionfake import FakeNotion

UID = 1


@pytest.fixture(autouse=True)
def _schema(db):
    yield


@pytest.fixture
def fake():
    return FakeNotion()


@pytest.fixture
def api(fake):
    return fake.executor()[0].api
