"""FastAPI app for the `api` role: webhooks + health. Never calls an LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI

from mavis.api.ratelimit import webhook_rate_limit
from mavis.api.routes import connect, health, integrations, oauth, slack, telegram
from mavis.bus import get_bus
from mavis.channels import telegram_webhook
from mavis.channels.test_sink import active_test_chat
from mavis.config import get_settings
from mavis.logging import configure_logging
from mavis.store.db import dispose_engine, init_db
from mavis.tools.integrations import close_provider
from mavis.tools.integrations.wiring import register_integrations

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    s = get_settings()
    if s.telegram_mode == "webhook" and not s.telegram_webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")
    if s.env == "prod" and not s.allowed_telegram_chat_ids:
        raise RuntimeError("ALLOWED_TELEGRAM_CHAT_IDS is required when ENV=prod")
    if s.live_test_enabled and active_test_chat(s) is None:
        log.error("live_test.disabled_at_startup")  # misconfigured: the reason is logged, path stays off
    if s.is_sqlite:
        await init_db()  # Postgres schemas are managed by `mavis migrate`
    register_integrations()
    app.state.bus = get_bus()
    if s.telegram_mode == "webhook" and s.telegram_bot_token:
        try:
            await telegram_webhook.set_webhook()
            log.info("telegram.webhook_set", path=telegram_webhook.WEBHOOK_PATH)
        except Exception as exc:  # noqa: BLE001 - the api must still boot; /readyz and logs surface it
            log.error("telegram.webhook_failed", error=str(exc))
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
    app.include_router(telegram.router, dependencies=[Depends(webhook_rate_limit)])
    app.include_router(connect.router)
    app.include_router(oauth.router)
    app.include_router(integrations.router, dependencies=[Depends(webhook_rate_limit)])
    app.include_router(slack.router)  # signature-verified; Slack bursts from shared IPs exceed the IP limit
    return app
