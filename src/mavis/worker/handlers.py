"""Default handler wiring for this phase. Later phases add their registrations here."""

from __future__ import annotations

from mavis.agents import simple_turn
from mavis.domain.events import EventType
from mavis.initiative.wiring import wire_initiative
from mavis.memory import jobs as memory_jobs
from mavis.tools.integrations.wiring import register_integrations
from mavis.worker.runner import register_event_handler


def register_default_handlers() -> None:
    register_event_handler(EventType.USER_MESSAGE, simple_turn.run_turn)
    memory_jobs.register()
    wire_initiative()
    register_integrations()  # after wire_initiative: replaces its CONNECTION_CHANGED/TASK_COMPLETED
