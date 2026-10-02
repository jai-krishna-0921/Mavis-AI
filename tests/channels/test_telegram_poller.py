import asyncio
from types import SimpleNamespace

from zento.channels.telegram_poller import run_polling


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
