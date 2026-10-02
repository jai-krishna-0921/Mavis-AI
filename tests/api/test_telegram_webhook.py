import httpx
import pytest

from zento.api.app import create_app
from zento.config import get_settings
from zento.domain.events import EventType, Trust
from zento.store.repo import users


def update(update_id: int, chat_id: int = 100, text: str = "hi", **extra) -> dict:
    msg = {"message_id": 5, "date": 1790930000, "chat": {"id": chat_id, "type": "private"},
           "from": {"id": chat_id, "first_name": "Jai"}, "text": text}
    msg.update(extra)
    return {"update_id": update_id, "message": msg}


@pytest.fixture
async def client(db, bus):
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _published(bus) -> list:
    items = []
    while not bus._events.empty():
        event, _ = bus._events.get_nowait()
        bus._events.task_done()
        items.append(event)
    return items


async def test_message_update_published_as_user_message(client, bus) -> None:
    r = await client.post("/webhooks/telegram", json=update(1, text="hello"))
    assert r.status_code == 200 and r.json() == {"ok": True, "published": True}
    [event] = await _published(bus)
    user = await users.get_by_chat(100)
    assert event.id == "tg:update:1" and event.type is EventType.USER_MESSAGE
    assert event.user_id == user.id and user.name == "Jai"
    assert event.payload["text"] == "hello" and event.trust is Trust.USER
    assert event.occurred_at.tzinfo is not None


async def test_duplicate_update_published_once(client, bus) -> None:
    await client.post("/webhooks/telegram", json=update(2))
    r = await client.post("/webhooks/telegram", json=update(2))
    assert r.json()["published"] is False
    assert len(await _published(bus)) == 1


async def test_start_command_and_document(client, bus) -> None:
    await client.post("/webhooks/telegram", json=update(3, text="/start@Mavis247_bot"))
    await client.post("/webhooks/telegram", json=update(
        4, text=None, caption="read this",
        document={"file_id": "F1", "file_name": "cv.pdf", "mime_type": "application/pdf", "file_size": 12},
    ))
    start, doc = await _published(bus)
    assert start.payload["command"] == "start"
    assert doc.payload["text"] == "read this"
    assert doc.payload["file"] == {"file_id": "F1", "file_name": "cv.pdf", "mime_type": "application/pdf",
                                   "size": 12}


async def test_photo_uses_largest_size(client, bus) -> None:
    await client.post("/webhooks/telegram", json=update(
        5, text=None, photo=[{"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
                             {"file_id": "big", "file_unique_id": "b", "width": 1280, "height": 1280}],
    ))
    [event] = await _published(bus)
    assert event.payload["file"]["file_id"] == "big"


async def test_callback_query_published_as_button_pressed(client, bus) -> None:
    body = {"update_id": 6, "callback_query": {
        "id": "cq1", "from": {"id": 100, "first_name": "Jai"}, "data": "appr:1:yes",
        "message": {"message_id": 9, "date": 1790930000, "chat": {"id": 100, "type": "private"}},
    }}
    await client.post("/webhooks/telegram", json=body)
    [event] = await _published(bus)
    assert event.type is EventType.BUTTON_PRESSED
    assert event.payload == {"data": "appr:1:yes", "callback_query_id": "cq1", "message_id": 9}


async def test_secret_token_enforced(client, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    get_settings.cache_clear()
    assert (await client.post("/webhooks/telegram", json=update(7))).status_code == 403
    r = await client.post("/webhooks/telegram", json=update(7),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
    assert r.status_code == 200


async def test_disallowed_chat_ignored(client, bus, monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_TELEGRAM_CHAT_IDS", "[999]")
    get_settings.cache_clear()
    r = await client.post("/webhooks/telegram", json=update(8, chat_id=100))
    assert r.json()["published"] is False
    assert await _published(bus) == []
    assert await users.get_by_chat(100) is None


async def test_unknown_update_type_ignored(client, bus) -> None:
    r = await client.post("/webhooks/telegram", json={"update_id": 9, "poll": {}})
    assert r.json()["published"] is False


async def test_webhook_mode_without_secret_refuses_to_start(settings, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="TELEGRAM_WEBHOOK_SECRET"):
        async with create_app().router.lifespan_context(create_app()):
            pass


@pytest.mark.parametrize(("text", "command"), [
    ("/", None), ("/ hi", None), ("/start", "start"), ("/start@Mavis247_bot hello", "start"),
    ("/Start", "start"), ("/start!", None),
])
async def test_command_parsing(client, bus, text, command) -> None:
    r = await client.post("/webhooks/telegram", json=update(20, text=text))
    assert r.json()["published"] is True
    [event] = await _published(bus)
    assert event.payload.get("command") == command
    assert event.payload["text"] == text
