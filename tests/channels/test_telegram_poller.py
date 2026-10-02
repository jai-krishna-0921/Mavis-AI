import asyncio
from types import SimpleNamespace

from telegram.error import NetworkError

from zento.channels.telegram_poller import run_polling

_real_sleep = asyncio.sleep


async def _fast_sleep(delay: float) -> None:
    await _real_sleep(0)


class PollingBot:
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = batches
        self.offsets: list = []
        self.webhook_deleted = False

    async def initialize(self) -> None:
        pass

    async def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self.webhook_deleted = True

    async def get_updates(self, offset=None, timeout=0, allowed_updates=None):  # noqa: ASYNC109 - mirrors telegram.Bot
        self.offsets.append(offset)
        if not self.batches:
            await asyncio.sleep(3600)
        return [SimpleNamespace(update_id=d["update_id"], to_dict=lambda d=d: d) for d in self.batches.pop(0)]


async def test_poller_feeds_ingest_and_advances_offset(db, bus) -> None:
    upd = {"update_id": 41, "message": {"message_id": 1, "date": 1790930000,
                                        "chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                                        "text": "yo"}}
    bot = PollingBot([[upd]])
    task = asyncio.create_task(run_polling(bus, "token", bot=bot))
    for _ in range(100):
        if len(bot.offsets) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert bot.webhook_deleted
    assert bot.offsets[:2] == [None, 42]
    event, _ = bus._events.get_nowait()
    assert event.id == "tg:update:41"


async def test_failed_ingest_is_retried_not_lost(db, bus, monkeypatch) -> None:
    from zento.channels import telegram_poller

    monkeypatch.setattr(telegram_poller.asyncio, "sleep", _fast_sleep)
    real = telegram_poller.ingest_update
    calls = {"n": 0}

    async def flaky(data, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return await real(data, b)

    monkeypatch.setattr(telegram_poller, "ingest_update", flaky)
    upd = {"update_id": 50, "message": {"message_id": 1, "date": 1790930000,
                                        "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "x"}}
    bot = PollingBot([[upd], [upd]])
    task = asyncio.create_task(run_polling(bus, "token", bot=bot))
    for _ in range(200):
        if len(bot.offsets) >= 3:
            break
        await _real_sleep(0.01)
    task.cancel()
    assert bot.offsets[:3] == [None, None, 51]
    assert bus._events.qsize() == 1
    event, _ = bus._events.get_nowait()
    assert event.id == "tg:update:50"


async def test_startup_retries_on_transient_network_errors(db, bus, monkeypatch) -> None:
    from zento.channels import telegram_poller

    monkeypatch.setattr(telegram_poller.asyncio, "sleep", _fast_sleep)

    class FlakeInitBot(PollingBot):
        def __init__(self, batches):
            super().__init__(batches)
            self.init_calls = 0

        async def initialize(self):
            self.init_calls += 1
            if self.init_calls <= 2:
                raise NetworkError("transient network error")

    upd = {"update_id": 30, "message": {"message_id": 1, "date": 1790930000,
                                        "chat": {"id": 7, "type": "private"},
                                        "from": {"id": 7}, "text": "hi"}}
    bot = FlakeInitBot([[upd]])
    task = asyncio.create_task(run_polling(bus, "token", bot=bot))
    for _ in range(200):
        if len(bot.offsets) >= 2:
            break
        await _real_sleep(0.01)
    task.cancel()
    assert bot.init_calls == 3
    assert bot.webhook_deleted
    assert bot.offsets[:2] == [None, 31]
    event, _ = bus._events.get_nowait()
    assert event.id == "tg:update:30"


async def test_poller_raises_on_invalid_token_at_startup(db, bus) -> None:
    import pytest
    from telegram.error import InvalidToken

    class Bad(PollingBot):
        async def initialize(self) -> None:
            raise InvalidToken("nope")

    with pytest.raises(InvalidToken):
        await run_polling(bus, "token", bot=Bad([]))


async def test_poller_raises_on_invalid_token_in_get_updates(db, bus) -> None:
    import pytest
    from telegram.error import InvalidToken

    class Bad(PollingBot):
        async def get_updates(self, **kw):
            raise InvalidToken("nope")

    with pytest.raises(InvalidToken):
        await run_polling(bus, "token", bot=Bad([]))
