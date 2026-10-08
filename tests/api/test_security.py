import httpx
import pytest

from mavis.api import ratelimit
from mavis.api.app import create_app
from mavis.config import get_settings

H = {"X-Telegram-Bot-Api-Secret-Token": "shh"}


def upd(chat_id: int, update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {
        "message_id": 5, "date": 1790930000, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "first_name": "Jai"}, "text": "hey"}}


@pytest.fixture
async def client(db, bus, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[111]")
    monkeypatch.setenv("ENV", "prod")
    get_settings.cache_clear()
    monkeypatch.setattr(ratelimit, "_limiter", None)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


@pytest.mark.parametrize("path", ["/telegram/webhook", "/webhooks/telegram"])
async def test_rejects_missing_or_wrong_secret(client, bus, path) -> None:
    assert (await client.post(path, json=upd(111))).status_code == 403
    r = await client.post(path, json=upd(111), headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 403
    assert bus._events.empty()


async def test_non_allowlisted_chat_publishes_nothing(client, bus) -> None:
    r = await client.post("/telegram/webhook", json=upd(999), headers=H)
    assert r.status_code == 200 and r.json()["published"] == 0
    assert bus._events.empty()


async def test_allowlisted_chat_is_published(client, bus) -> None:
    r = await client.post("/telegram/webhook", json=upd(111, 42), headers=H)
    assert r.status_code == 200 and r.json()["published"] >= 1


async def test_rate_limiter_counts_per_key() -> None:
    rl = ratelimit.RateLimiter(limit=3, window_s=60)
    assert [await rl.hit("1.2.3.4") for _ in range(4)] == [True, True, True, False]
    assert await rl.hit("5.6.7.8") is True


async def test_webhook_returns_429_when_limited(client, monkeypatch) -> None:
    monkeypatch.setattr(ratelimit, "_limiter", ratelimit.RateLimiter(limit=1, window_s=60))
    assert (await client.post("/telegram/webhook", json=upd(111, 1), headers=H)).status_code == 200
    assert (await client.post("/telegram/webhook", json=upd(111, 2), headers=H)).status_code == 429


async def test_prod_requires_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[]")
    get_settings.cache_clear()
    from mavis.api.app import lifespan

    with pytest.raises(RuntimeError, match="ALLOWED_TELEGRAM_CHAT_IDS"):
        async with lifespan(create_app()):
            pass
    get_settings.cache_clear()
