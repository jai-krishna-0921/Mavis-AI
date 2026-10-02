"""FastAPI app for the `api` role: webhooks + health. Never calls an LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from zento.api.routes import health, telegram
from zento.bus import get_bus
from zento.config import get_settings
from zento.logging import configure_logging
from zento.store.db import dispose_engine, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    s = get_settings()
    if s.telegram_mode == "webhook" and not s.telegram_webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")
    if s.is_sqlite:
        await init_db()  # Postgres schemas are managed by `zento migrate`
    app.state.bus = get_bus()
    yield
    await get_bus().close()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Zento", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(telegram.router)
    return app
