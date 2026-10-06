import asyncio

from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.worker.runner import handle_event, register_event_handler


def make(event_type: EventType, event_id: str, user_id: int = 1) -> Event:
    return Event(id=event_id, user_id=user_id, type=event_type, occurred_at=timeutil.now(),
                 source="test", payload={}, trust=Trust.USER if event_type is EventType.USER_MESSAGE
                 else Trust.SYSTEM)


async def test_slow_initiative_event_does_not_block_user_reply(settings) -> None:
    release = asyncio.Event()
    log: list[str] = []

    async def slow_initiative(event: Event) -> None:
        log.append("initiative:start")
        await release.wait()
        log.append("initiative:end")

    async def reply(event: Event) -> None:
        log.append("reply")

    register_event_handler(EventType.WAKEUP, slow_initiative)
    register_event_handler(EventType.USER_MESSAGE, reply)
    wake = asyncio.create_task(handle_event(make(EventType.WAKEUP, "w1")))
    await asyncio.sleep(0.01)
    await asyncio.wait_for(handle_event(make(EventType.USER_MESSAGE, "m1")), timeout=2)
    assert log == ["initiative:start", "reply"]
    release.set()
    await wake
    assert log[-1] == "initiative:end"


async def test_initiative_events_for_one_user_are_serialised(settings) -> None:
    log: list[str] = []

    async def slow(event: Event) -> None:
        log.append(f"start:{event.id}")
        await asyncio.sleep(0.03)
        log.append(f"end:{event.id}")

    register_event_handler(EventType.WAKEUP, slow)
    await asyncio.gather(handle_event(make(EventType.WAKEUP, "a")), handle_event(make(EventType.WAKEUP, "b")))
    assert log == ["start:a", "end:a", "start:b", "end:b"]
