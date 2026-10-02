from datetime import UTC, datetime

from zento.channels.outbox_sender import OutboxSender
from zento.domain.events import Event, EventType, Trust
from zento.store.repo import users
from zento.worker.handlers import register_default_handlers
from zento.worker.runner import handle_event


async def test_default_handlers_route_user_messages_to_simple_turn(
    db, channel, fake_llm, memory, bus
) -> None:
    register_default_handlers()
    register_default_handlers()  # idempotent
    user, _ = await users.get_or_create_by_chat(5, "Jai")
    fake_llm.push_text("Hello!")
    await handle_event(Event(id="tg:update:9", user_id=user.id, type=EventType.USER_MESSAGE,
                             occurred_at=datetime.now(UTC), source="telegram",
                             payload={"text": "hi"}, trust=Trust.USER))
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Hello!"]
