"""The dashboard API, mounted at /api/v1 as its own small app so its errors always have the
{"error", "message"} shape and it answers 404 unless DASHBOARD_ENABLED."""

from __future__ import annotations

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mavis.api.dashboard import account, auth, common, connectors, invites, me, public_config, vault
from mavis.api.dashboard.common import DashError

log = structlog.get_logger(__name__)

_PLAIN = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
          405: "method_not_allowed", 429: "rate_limited"}
_TEXT = {401: "Please sign in to continue.", 404: "Not found.", 405: "That is not allowed here.",
         429: "Too many requests. Wait a minute and try again."}


def _json(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def create_dashboard_app() -> FastAPI:
    app = FastAPI(title="Mavis dashboard API", dependencies=[Depends(common.require_enabled)],
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(DashError)
    async def _dash(_: Request, exc: DashError) -> JSONResponse:
        return _json(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _json(422, "invalid_request", "Some of what was sent is not valid. Check it and try again.")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _json(exc.status_code, _PLAIN.get(exc.status_code, "error"),
                     _TEXT.get(exc.status_code, "Something went wrong. Please try again."))

    @app.exception_handler(Exception)
    async def _boom(_: Request, exc: Exception) -> JSONResponse:
        log.error("dashboard.unhandled", error=type(exc).__name__)  # never the message: it may hold user text
        return _json(500, "server_error", "Something went wrong on our side. Please try again.")

    for module in (public_config, auth, me, connectors, vault, invites, account):
        app.include_router(module.router)
    return app
