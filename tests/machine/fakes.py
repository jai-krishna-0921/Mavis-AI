"""Shared doubles for the machine tests."""

from __future__ import annotations


class RecordingCards:
    """Stands in for ProgressCards: records every call in order."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.started: set[int] = set()

    def has_card(self, task_id: int) -> bool:
        return task_id in self.started

    async def start(self, task_id, user_id, goal, steps, *, tainted):
        self.started.add(task_id)
        self.calls.append(("start", task_id, goal, [s.id for s in steps], tainted))

    async def step_started(self, task_id, step_id):
        self.calls.append(("step_started", task_id, step_id))

    async def step_finished(self, task_id, step_id, state):
        self.calls.append(("step_finished", task_id, step_id, state))

    async def tool_called(self, task_id, label):
        self.calls.append(("tool", task_id, label))

    async def file_sent(self, task_id, n=1):
        self.calls.append(("file", task_id, n))

    async def set_live_url(self, task_id, url):
        self.calls.append(("live", task_id, url))

    async def finalize(self, task_id, final):
        self.calls.append(("final", task_id, final))

    async def flush(self, task_id):
        return None

    def kinds(self, task_id: int | None = None) -> list[str]:
        return [c[0] for c in self.calls if task_id is None or c[1] == task_id]
