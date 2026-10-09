"""Default handler wiring for the worker roles (`mavis dev`, `mavis worker`, `mavis chat`).

Order matters: the initiative engine first (it claims every non-chat event type), then integrations
(replaces CONNECTION_CHANGED / TASK_COMPLETED, sets the tool registry's policy hooks, owns the connect
interrupt), then Phase 4 (chat turns, approval buttons, task jobs, TASK_PROGRESS, system wakeups), then
attention last (replaces EMAIL_RECEIVED, appends to TASK_COMPLETED).
Every call re-registers everything and appends nothing twice.
"""

from __future__ import annotations

from mavis.access import budgets, deletion, invite_commands
from mavis.access.commands import register_command_gate
from mavis.access.gate import register_access_gate
from mavis.agents import clear, onboarding
from mavis.agents.wiring import register as register_phase4
from mavis.attention.wiring import register_attention
from mavis.initiative.wiring import wire_initiative
from mavis.memory import jobs as memory_jobs
from mavis.tools.integrations.wiring import register_integrations
from mavis.tools.registry import get_registry
from mavis.web import login_gate


def register_default_handlers() -> None:
    memory_jobs.register()
    wire_initiative()
    # after wire_initiative: replaces its CONNECTION_CHANGED/TASK_COMPLETED handlers
    register_integrations(get_registry())
    register_phase4()
    register_attention()  # last: replaces EMAIL_RECEIVED, appends to TASK_COMPLETED
    register_access_gate()  # before any handler runs: strangers never reach routing
    register_command_gate()
    login_gate.register()
    invite_commands.register()
    budgets.register()
    deletion.register()
    clear.register()
    onboarding.register()
