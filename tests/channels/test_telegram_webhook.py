import json

import pytest
import respx
from httpx import Response
from typer.testing import CliRunner

from mavis.channels import telegram_webhook as tw
from mavis.cli import app
from mavis.config import get_settings

OK = Response(200, json={"ok": True, "result": True})
API = "https://api.telegram.org/botTEST:TOKEN"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://1-2-3-4.sslip.io/")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@respx.mock
async def test_set_webhook_sends_url_secret_and_updates() -> None:
    route = respx.post(f"{API}/setWebhook").mock(return_value=OK)
    assert await tw.set_webhook() is True
    assert json.loads(route.calls.last.request.content) == {
        "url": "https://1-2-3-4.sslip.io/telegram/webhook",
        "secret_token": "shh",
        "allowed_updates": ["message", "edited_message", "callback_query"],
        "drop_pending_updates": False,
    }


@respx.mock
async def test_set_webhook_raises_on_telegram_error() -> None:
    respx.post(f"{API}/setWebhook").mock(
        return_value=Response(400, json={"ok": False, "description": "bad url"}))
    with pytest.raises(RuntimeError, match="bad url"):
        await tw.set_webhook()


async def test_set_webhook_requires_https(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="https"):
        await tw.set_webhook()


@respx.mock
async def test_delete_webhook_keeps_pending_updates() -> None:
    route = respx.post(f"{API}/deleteWebhook").mock(return_value=OK)
    await tw.delete_webhook()
    assert json.loads(route.calls.last.request.content) == {"drop_pending_updates": False}


@respx.mock
def test_cli_commands() -> None:
    respx.post(f"{API}/deleteWebhook").mock(return_value=OK)
    respx.post(f"{API}/getWebhookInfo").mock(
        return_value=Response(200, json={"ok": True, "result": {"url": "https://x/telegram/webhook"}}))
    r = CliRunner()
    assert r.invoke(app, ["telegram", "delete-webhook"]).exit_code == 0
    out = r.invoke(app, ["telegram", "info"])
    assert out.exit_code == 0 and "telegram/webhook" in out.output
