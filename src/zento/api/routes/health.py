from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from zento.bus import get_redis
from zento.store import db

router = APIRouter()
ReadinessCheck = Callable[[], Awaitable[bool]]
_checks: dict[str, ReadinessCheck] = {}


def register_readiness_check(name: str, fn: ReadinessCheck) -> None:
    """Later phases add Neo4j / Qdrant / provider checks here."""
    _checks[name] = fn


async def _bus_ping() -> bool:
    client = get_redis()
    return True if client is None else bool(await client.ping())


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready() -> JSONResponse:
    results: dict[str, bool] = {}
    for name, fn in {"database": db.ping, "bus": _bus_ping, **_checks}.items():
        try:
            results[name] = bool(await asyncio.wait_for(fn(), timeout=3))
        except Exception:  # noqa: BLE001 - any failure means not ready
            results[name] = False
    ok = all(results.values())
    body = {"status": "ok" if ok else "degraded", "checks": results}
    return JSONResponse(body, status_code=200 if ok else 503)
