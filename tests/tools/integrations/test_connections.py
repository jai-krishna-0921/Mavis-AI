import pytest

from mavis.domain.errors import ConnectionRequired
from mavis.domain.integrations import ConnectionState
from mavis.domain.policy import Capability
from mavis.tools.integrations.connections import ConnectionCache
from tests.tools.integrations.fakes import FakeProvider


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_cache_hit_within_ttl_and_expiry():
    p, clock = FakeProvider(), Clock()
    c = ConnectionCache(p, ttl_s=60, clock=clock)
    await c.status(1)
    await c.status(1)
    assert p.status_calls == 1
    clock.t += 61
    await c.status(1)
    assert p.status_calls == 2


async def test_fresh_and_invalidate_bypass_cache():
    p = FakeProvider()
    c = ConnectionCache(p, ttl_s=60)
    await c.status(1)
    await c.status(1, fresh=True)
    c.invalidate(1)
    await c.status(1)
    assert p.status_calls == 3


async def test_ensure_raises_connection_required():
    p = FakeProvider()
    c = ConnectionCache(p, ttl_s=60)
    with pytest.raises(ConnectionRequired) as exc:
        await c.ensure(1, Capability.GMAIL, "check and handle your email")
    assert exc.value.capability is Capability.GMAIL
    p.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    c.invalidate(1)
    await c.ensure(1, Capability.GMAIL, "x")
    assert await c.is_active(1, Capability.GMAIL)


def test_get_provider_is_composio(settings, monkeypatch):
    from mavis.config import get_settings
    from mavis.tools.integrations import get_provider
    from mavis.tools.integrations.composio import ComposioProvider

    monkeypatch.setenv("COMPOSIO_API_KEY", "k")
    get_settings.cache_clear()
    get_provider.cache_clear()
    assert isinstance(get_provider(), ComposioProvider)
    get_provider.cache_clear()
    get_settings.cache_clear()


async def test_invalidate_during_inflight_fetch_does_not_cache_stale_result():
    import asyncio

    p = FakeProvider()
    gate, started = asyncio.Event(), asyncio.Event()
    real = p.status

    async def slow(user):
        out = await real(user)
        started.set()
        await gate.wait()
        return out

    p.status = slow
    c = ConnectionCache(p, ttl_s=60)
    task = asyncio.create_task(c.status(1))
    await started.wait()
    c.invalidate(1)
    gate.set()
    await task
    p.status = real
    p.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    assert (await c.status(1))["gmail"] is ConnectionState.ACTIVE


async def test_status_returns_copy_callers_cannot_mutate():
    p = FakeProvider()
    c = ConnectionCache(p, ttl_s=60)
    first = await c.status(1)
    first["gmail"] = ConnectionState.ACTIVE
    second = await c.status(1)
    assert second["gmail"] is ConnectionState.NONE
    second["gmail"] = ConnectionState.ACTIVE
    assert (await c.status(1))["gmail"] is ConnectionState.NONE
