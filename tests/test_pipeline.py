"""End to end: Telegram update -> ingest -> bus -> worker -> run_turn -> outbox -> sender -> channel."""

import asyncio

from zento.channels.outbox_sender import OutboxSender
from zento.channels.telegram_updates import ingest_update
from zento.domain.events import EventType
from zento.worker.runner import register_event_handler, run_worker


def _update(update_id: int = 900, text: str = "hello there") -> dict:
    return {"update_id": update_id, "message": {
        "message_id": 5, "date": 1790930000, "chat": {"id": 321, "type": "private"},
        "from": {"id": 321, "first_name": "Jai"}, "text": text}}


async def test_update_flows_to_exactly_one_reply(db, bus, channel, fake_llm) -> None:
    from zento.agents.simple_turn import run_turn

    register_event_handler(EventType.USER_MESSAGE, run_turn)
    fake_llm.push_text("Hey Jai!\n\nWhat's up?")
    worker = asyncio.create_task(run_worker(bus, "test"))
    try:
        assert await ingest_update(_update(), bus) is True
        assert await ingest_update(_update(), bus) is False  # same update again: deduped at the bus
        await bus.wait_idle()
        sender = OutboxSender(channel)
        await sender.run_once()
        assert channel.texts == ["Hey Jai!", "What's up?"]

        # a redelivered copy (e.g. after a restart with an empty dedupe set) still yields no new reply
        bus._seen.clear()
        assert await ingest_update(_update(), bus) is True
        await bus.wait_idle()
        await sender.run_once()
        assert channel.texts == ["Hey Jai!", "What's up?"]
        assert len(fake_llm.calls) == 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
