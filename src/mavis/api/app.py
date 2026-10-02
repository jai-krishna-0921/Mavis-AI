"""FastAPI app for the `api` role: webhooks + health. Never calls an LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mavis.api.routes import connect, health, integrations, telegram
from mavis.bus import get_bus
from mavis.config import get_settings
from mavis.logging import configure_logging
from mavis.store.db import dispose_engine, init_db
from mavis.tools.integrations import close_provider
from mavis.tools.integrations.wiring import register_integrations


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    s = get_settings()
    if s.telegram_mode == "webhook" and not s.telegram_webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")
    if s.is_sqlite:
        await init_db()  # Postgres schemas are managed by `mavis migrate`
    register_integrations()
    app.state.bus = get_bus()
    try:
        yield
    finally:
        try:
            await get_bus().close()
        finally:
            try:
                await close_provider()
            finally:
                await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Mavis", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(telegram.router)
    app.include_router(connect.router)
    app.include_router(integrations.router)
    return app
