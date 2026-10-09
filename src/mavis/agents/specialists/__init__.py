"""Specialist registry. Phases 5 and 6 register more specialists here."""

from __future__ import annotations

from mavis.agents.specialists.base import Specialist

SPECIALISTS: dict[str, Specialist] = {}


def register_specialist(spec: Specialist) -> None:
    SPECIALISTS[spec.name] = spec


def get_specialist(name: str) -> Specialist:
    return SPECIALISTS[name]


def register_machine_specialists() -> None:
    from mavis.agents.specialists.analyst import ANALYST
    from mavis.agents.specialists.docs import DOCS

    for spec in (ANALYST, DOCS):
        register_specialist(spec)


def unregister_machine_specialists() -> None:
    """Flag off: the machine specialists are not in the planner's catalog."""
    for name in ("analyst", "docs", "operator"):
        SPECIALISTS.pop(name, None)


from mavis.agents.specialists.calendar import CALENDAR  # noqa: E402
from mavis.agents.specialists.inbox import INBOX  # noqa: E402
from mavis.agents.specialists.knowledge import KNOWLEDGE  # noqa: E402
from mavis.agents.specialists.research import RESEARCH  # noqa: E402

for _spec in (RESEARCH, KNOWLEDGE, INBOX, CALENDAR):
    register_specialist(_spec)
