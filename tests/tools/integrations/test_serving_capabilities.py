# ruff: noqa: E501, F811
# tests/tools/integrations/test_serving_capabilities.py
import pytest

from mavis.domain.errors import ConnectionRequired
from mavis.domain.integrations import ConnectionState as S
from mavis.domain.policy import Capability as C
from mavis.tools.integrations.actions import serving
from mavis.tools.integrations.connections import ConnectionCache
from tests.tools.integrations.fakes import FakeProvider


def test_aliases():
    assert serving(C.GMAIL) == (C.GMAIL, C.OUTLOOK) and serving(C.OUTLOOK_CALENDAR) == (C.CALENDAR, C.OUTLOOK_CALENDAR)
    assert serving(C.SLACK) == (C.SLACK,)


def cache_with(**states):
    provider = FakeProvider()
    for cap, state in states.items():
        provider.set_state(1, C(cap), state)
    return ConnectionCache(provider)


async def test_outlook_only_user_passes_a_gmail_declared_tool():
    await cache_with(outlook=S.ACTIVE).ensure_any(1, serving(C.GMAIL), "check your email")


async def test_nothing_connected_asks_for_the_first_alias_not_revoked():
    with pytest.raises(ConnectionRequired) as e:
        await cache_with().ensure_any(1, serving(C.GMAIL), "r")
    assert e.value.capability is C.GMAIL and not e.value.revoked


async def test_failed_outlook_asks_to_reconnect_outlook():
    with pytest.raises(ConnectionRequired) as e:
        await cache_with(outlook=S.FAILED).ensure_any(1, serving(C.GMAIL), "r")
    assert e.value.capability is C.OUTLOOK and e.value.revoked


async def test_google_active_and_outlook_failed_is_still_fine():
    await cache_with(gmail=S.ACTIVE, outlook=S.FAILED).ensure_any(1, serving(C.GMAIL), "r")
