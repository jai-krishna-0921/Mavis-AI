"""Default handler wiring for the worker roles (`mavis dev`, `mavis worker`, `mavis chat`).

Order matters: the initiative engine first (it claims every non-chat event type), then integrations
(replaces CONNECTION_CHANGED / TASK_COMPLETED, sets the tool registry's policy hooks, owns the connect
interrupt), then Phase 4 (chat turns, approval buttons, task jobs, TASK_PROGRESS, system wakeups).
Every call re-registers everything and appends nothing twice.
"""

from __future__ import annotations

from mavis.agents.wiring import register as register_phase4
from mavis.initiative.wiring import wire_initiative
from mavis.memory import jobs as memory_jobs
from mavis.tools.integrations.wiring import register_integrations
from mavis.tools.registry import get_registry


def register_default_handlers() -> None:
    memory_jobs.register()
    wire_initiative()
    # after wire_initiative: replaces its CONNECTION_CHANGED/TASK_COMPLETED handlers
    register_integrations(get_registry())
    register_phase4()
