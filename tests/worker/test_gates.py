from __future__ import annotations

from datetime import UTC, datetime

from mavis.domain.events import Event, EventType, Trust
from mavis.worker import gates, runner


def _ev(text: str, uid: int = 1, etype: EventType = EventType.USER_MESSAGE) -> Event:
    return Event(id=f"t:{text}:{uid}", user_id=uid, type=etype,
                 occurred_at=datetime(2026, 10, 8, tzinfo=UTC), source="test", payload={"text": text},
                 trust=Trust.USER)


async def test_a_false_gate_stops_every_handler_and_order_is_respected(settings):
    seen: list[str] = []

    async def first(e):
        seen.append("first")
        return "stop" not in e.payload["text"]

    async def second(e):
        seen.append("second")
        return True

    async def handler(e):
        seen.append("handler")

    gates.register_event_gate("second", second, order=20)
    gates.register_event_gate("first", first, order=10)
    runner.register_event_handler(EventType.USER_MESSAGE, handler)
    await runner.handle_event(_ev("go"))
    await runner.handle_event(_ev("stop"))
    assert seen == ["first", "second", "handler", "first"]


async def test_no_gates_means_unchanged(settings):
    calls = []

    async def handler(e):
        calls.append(e.id)

    runner.register_event_handler(EventType.USER_MESSAGE, handler)
    await runner.handle_event(_ev("x"))
    assert calls == ["t:x:1"]


async def test_gate_only_event_types_reach_the_gates_without_a_handler(settings):
    seen: list[EventType] = []

    async def gate(e):
        seen.append(e.type)
        return False

    gates.register_event_gate("g", gate)
    await runner.handle_event(_ev("", etype=EventType.RATE_LIMITED))
    await runner.handle_event(_ev("", etype=EventType.CHAT_MEMBER))
    await runner.handle_event(_ev("", etype=EventType.WAKEUP))  # no handler, not gate-only: ignored
    assert seen == [EventType.RATE_LIMITED, EventType.CHAT_MEMBER]


async def test_clear_handlers_also_clears_gates(settings):
    gates.register_event_gate("g", lambda e: None)
    runner.clear_handlers()
    assert await gates.run_gates(_ev("x")) is True
