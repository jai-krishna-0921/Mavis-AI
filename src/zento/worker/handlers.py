"""Default handler wiring for this phase. Later phases add their registrations here."""

from __future__ import annotations

from zento.agents import simple_turn
from zento.domain.events import EventType
from zento.memory import jobs as memory_jobs
from zento.worker.runner import register_event_handler


def register_default_handlers() -> None:
    register_event_handler(EventType.USER_MESSAGE, simple_turn.run_turn)
    memory_jobs.register()
