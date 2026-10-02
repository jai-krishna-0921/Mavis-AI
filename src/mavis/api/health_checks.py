"""Dependency readiness probes. Each check returns None (ok), "skipped" (not configured) or raises."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from mavis.config import get_settings

Check = Callable[[], Awaitable[str | None]]


async def check_db() -> str | None:
    from mavis.store import db

    await db.ping()
    return None


async def check_redis() -> str | None:
    from mavis.bus import get_redis

    client = get_redis()
    if client is None:
        return "skipped"
    await client.ping()
    return None


async def check_qdrant() -> str | None:
    url = get_settings().qdrant_url
    if not url:
        return "skipped"
    async with httpx.AsyncClient(timeout=3) as c:
        (await c.get(f"{url.rstrip('/')}/readyz")).raise_for_status()
    return None


async def check_neo4j() -> str | None:
    s = get_settings()
    if not s.neo4j_uri:
        return "skipped"
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    try:
        await driver.verify_connectivity()
    finally:
        await driver.close()
    return None


CHECKS: dict[str, Check] = {
    "db": check_db,
    "redis": check_redis,
    "qdrant": check_qdrant,
    "neo4j": check_neo4j,
}


async def _run(fn: Check, timeout_s: float) -> str:
    try:
        res = await asyncio.wait_for(fn(), timeout=timeout_s)
    except TimeoutError:
        return "timeout"
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        return f"error: {type(exc).__name__}"
    return res or "ok"


async def readiness(timeout_s: float = 3.0) -> tuple[bool, dict[str, str]]:
    names = list(CHECKS)
    results = await asyncio.gather(*(_run(CHECKS[n], timeout_s) for n in names))
    checks = dict(zip(names, results, strict=True))
    return all(v in ("ok", "skipped") for v in checks.values()), checks
