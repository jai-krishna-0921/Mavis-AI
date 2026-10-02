import pytest

from mavis.agents import simple_turn
from mavis.bus import set_bus
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import wiring
from mavis.initiative.wiring import build_initiative


@pytest.fixture
def init(recording_bus, fake_memory):
    async def no_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    i = build_initiative(recording_bus, fake_memory, embed=no_embed)
    wiring.set_current(i)
    set_bus(recording_bus)  # enqueue_learn publishes through get_bus()
    yield i
    set_bus(None)
    wiring.set_current(None)


async def test_turn_seeds_routine_and_tracks_question(user, clock, fake_llm, init):
    fake_llm.push_text("Hey Jai! What's one thing on your plate you'd rather not deal with?")
    await simple_turn.run_turn(Event(id="tg:update:10", user_id=user.id, type=EventType.USER_MESSAGE,
                                     occurred_at=timeutil.now(), source="telegram",
                                     payload={"text": "Hey!"}, trust=Trust.USER))
    assert any(lp.kind is LoopKind.ROUTINE for lp in await init.loops.active(user.id))
    assert len(await init.wakeups.pending(user.id, WakeupKind.USER_QUIET)) == 1


async def test_user_reply_cancels_pending_quiet(user, clock, fake_llm, init):
    await init.quiet.after_assistant_message(user.id, "What's on your plate?")
    fake_llm.push_text("Got it.")
    await simple_turn.run_turn(Event(id="tg:update:11", user_id=user.id, type=EventType.USER_MESSAGE,
                                     occurred_at=timeutil.now(), source="telegram",
                                     payload={"text": "Interview stuff"}, trust=Trust.USER))
    assert await init.wakeups.pending(user.id, WakeupKind.USER_QUIET) == []


async def test_hook_failure_never_breaks_the_reply(user, clock, fake_llm, init, channel, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("hook down")

    monkeypatch.setattr(init.routines, "on_user_message", boom)
    monkeypatch.setattr(init.quiet, "after_assistant_message", boom)
    fake_llm.push_text("Still here.")
    await simple_turn.run_turn(Event(id="tg:update:12", user_id=user.id, type=EventType.USER_MESSAGE,
                                     occurred_at=timeutil.now(), source="telegram",
                                     payload={"text": "Hi"}, trust=Trust.USER))
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    assert any("Still here." in str(s) for s in channel.sent)
