"""The task wall clock, extendable once the plan is known (machine plans get a longer one)."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar


class TaskClock:
    def __init__(self, timeout: asyncio.Timeout, base_s: float, max_s: float, started: float) -> None:
        self._timeout, self.base_s, self.max_s, self.started = timeout, base_s, max(max_s, base_s), started
        self.total_s = base_s

    def extend_to(self, total_s: float) -> float:
        """Make the task's whole budget `total_s` (clamped to [base, max]). Returns the budget in force."""
        self.total_s = min(max(float(total_s), self.base_s), self.max_s)
        self._timeout.reschedule(self.started + self.total_s)
        return self.total_s


current_clock: ContextVar[TaskClock | None] = ContextVar("current_clock", default=None)
