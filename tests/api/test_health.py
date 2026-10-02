import asyncio

import httpx
import pytest

from mavis.api import health_checks
from mavis.api.app import create_app


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


async def test_live_aliases(client) -> None:
    async with client as c:
        assert (await c.get("/health/live")).json() == {"status": "ok"}
        assert (await c.get("/healthz")).json() == {"status": "ok"}


async def test_ready_dev_skips_unconfigured_dependencies(db, bus, client) -> None:
    async with client as c:
        for path in ("/health/ready", "/readyz"):
            r = await c.get(path)
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["checks"]["db"] == "ok"
            assert body["checks"]["qdrant"] == "skipped"
            assert body["checks"]["neo4j"] == "skipped"


async def test_ready_503_when_a_check_errors(monkeypatch, client) -> None:
    async def ok() -> None:
        return None

    async def boom() -> None:
        raise ConnectionRefusedError

    monkeypatch.setattr(health_checks, "CHECKS", {"db": ok, "neo4j": boom})
    async with client as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"] == {"db": "ok", "neo4j": "error: ConnectionRefusedError"}


async def test_ready_reports_timeout_for_hanging_check(monkeypatch) -> None:
    async def hang() -> None:
        await asyncio.sleep(30)

    async def ok() -> None:
        return None

    monkeypatch.setattr(health_checks, "CHECKS", {"db": ok, "neo4j": hang})
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    ok_all, checks = await health_checks.readiness(timeout_s=0.2)
    assert loop.time() - t0 < 1.0
    assert ok_all is False
    assert checks == {"db": "ok", "neo4j": "timeout"}
