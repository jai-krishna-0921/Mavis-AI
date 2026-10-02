from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from mavis.api import health_checks

router = APIRouter()


@router.get("/health/live")
@router.get("/healthz")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
@router.get("/readyz")
async def ready() -> JSONResponse:
    ok, checks = await health_checks.readiness()  # via the module so tests can patch CHECKS
    return JSONResponse({"ok": ok, "status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)
