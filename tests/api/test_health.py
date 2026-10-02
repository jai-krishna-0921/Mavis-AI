import httpx

from mavis.api.app import create_app
from mavis.api.routes.health import register_readiness_check


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")


async def test_live_and_ready(db, bus) -> None:
    async with await _client() as c:
        assert (await c.get("/health/live")).json() == {"status": "ok"}
        r = await c.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": True, "bus": True}}


async def test_ready_degrades_when_a_check_fails(db, bus) -> None:
    async def broken() -> bool:
        raise RuntimeError("neo4j down")

    register_readiness_check("neo4j", broken)
    try:
        async with await _client() as c:
            r = await c.get("/health/ready")
        assert r.status_code == 503 and r.json()["checks"]["neo4j"] is False
    finally:
        from mavis.api.routes import health

        health._checks.pop("neo4j", None)
