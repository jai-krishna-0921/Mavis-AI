import pytest

from mavis.domain.events import EventType
from mavis.initiative import handler as handler_mod
from mavis.initiative import wiring
from mavis.loops.service import LoopService


@pytest.fixture(autouse=True)
def _reset():
    wiring.set_current(None)
    yield
    wiring.set_current(None)


def test_wire_initiative_hooks_memory_and_registers(monkeypatch, recording_bus, fake_memory):
    import mavis.bus
    import mavis.memory.service

    monkeypatch.setattr(mavis.bus, "get_bus", lambda: recording_bus)
    monkeypatch.setattr(mavis.memory.service, "get_memory", lambda: fake_memory)
    registered: list[EventType] = []
    monkeypatch.setattr(handler_mod, "register_event_handler", lambda t, h: registered.append(t))
    init = wiring.wire_initiative()
    assert wiring.wire_initiative() is init  # idempotent
    assert len(fake_memory.on_extraction) == 1
    assert isinstance(fake_memory.loops_reader, LoopService)
    assert EventType.USER_MESSAGE not in registered and EventType.BUTTON_PRESSED not in registered
    assert set(registered) == set(handler_mod.HANDLED_TYPES)
    assert EventType.EVENT_ENDED in registered and EventType.EMAIL_RECEIVED in registered


def test_wire_initiative_registers_again_after_handlers_are_cleared(monkeypatch, recording_bus, fake_memory):
    import mavis.bus
    import mavis.memory.service
    from mavis.worker import runner

    monkeypatch.setattr(mavis.bus, "get_bus", lambda: recording_bus)
    monkeypatch.setattr(mavis.memory.service, "get_memory", lambda: fake_memory)
    init = wiring.wire_initiative()
    runner.clear_handlers()
    assert wiring.wire_initiative() is init
    assert len(fake_memory.on_extraction) == 1  # hooked once per memory object
    assert init.handler.handle in runner._event_handlers[EventType.EVENT_ENDED]
