"""Test doubles for the integrations phase. No network, no real bus."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mavis.domain.errors import IntegrationError
from mavis.domain.events import Event, Job
from mavis.domain.integrations import ConnectionState, Toolkit, ToolResult, UserRef
from mavis.domain.messages import Outbound
from mavis.tools.integrations.actions import INTEGRATION_CAPABILITIES

NOW = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)  # Mon 10:00 IST


class FakeProvider:
    def __init__(self) -> None:
        self.states: dict[int, dict[str, ConnectionState]] = {}
        self.results: dict[str, ToolResult] = {}
        self.executed: list[tuple[int, str, dict]] = []
        self.links: list[tuple[int, str, str]] = []
        self.subscribed: list[tuple[int, str]] = []
        self.disconnected: list[tuple[int, str]] = []
        self.status_calls = 0
        self.fail_subscribe = False
        self.fail_link = False

    def set_state(self, user_id: int, capability: Any, state: ConnectionState) -> None:
        self.states.setdefault(user_id, {})[capability.value] = state

    async def catalog(self) -> list[Toolkit]:
        return [Toolkit(slug=c.value, name=c.value, description="") for c in INTEGRATION_CAPABILITIES]

    async def status(self, user: UserRef) -> dict[str, ConnectionState]:
        self.status_calls += 1
        out = {c.value: ConnectionState.NONE for c in INTEGRATION_CAPABILITIES}
        out.update(self.states.get(user.user_id, {}))
        return out

    async def connect_link(self, user: UserRef, toolkit: str, callback_url: str) -> str:
        if self.fail_link:
            raise IntegrationError("COMPOSIO_API_KEY is not set, so no account can be connected.")
        self.links.append((user.user_id, toolkit, callback_url))
        return f"https://connect.example/{toolkit}"

    async def disconnect(self, user: UserRef, toolkit: str) -> None:
        self.disconnected.append((user.user_id, toolkit))
        self.states.get(user.user_id, {}).pop(toolkit, None)

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        self.executed.append((user.user_id, action, args))
        return self.results.get(action, ToolResult(ok=True, data={"ok": action}))

    async def subscribe(self, user: UserRef, trigger: str, config: dict) -> str:
        if self.fail_subscribe:
            raise IntegrationError("Composio answered 404 for POST /trigger_instances/x/upsert")
        self.subscribed.append((user.user_id, trigger))
        return "ti_1"

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[Event]:
        return []


class FakeBus:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.jobs: list[Job] = []
        self._seen: set[str] = set()

    async def publish(self, event: Event) -> bool:
        if event.id in self._seen:
            return False
        self._seen.add(event.id)
        self.events.append(event)
        return True

    async def enqueue(self, job: Job) -> None:
        self.jobs.append(job)

    async def consume_events(self, group, consumer, handler) -> None:  # pragma: no cover
        raise NotImplementedError

    async def consume_jobs(self, group, consumer, handler) -> None:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None


class FakeState:
    def __init__(self) -> None:
        self.data: dict[int, dict] = {}

    async def get(self, user_id: int) -> dict:
        return dict(self.data.get(user_id, {}))

    async def update(self, user_id: int, patch: dict) -> dict:
        merged = {**self.data.get(user_id, {}), **patch}
        self.data[user_id] = merged
        return merged


class Recorder:
    """Stands in for notify (Outbound) and schedule (user_id, at, reason, kind) callables."""

    def __init__(self) -> None:
        self.sent: list[Outbound] = []
        self.scheduled: list[tuple[int, datetime, str, str]] = []

    async def notify(self, msg: Outbound) -> None:
        self.sent.append(msg)

    async def schedule(self, user_id: int, at: datetime, reason: str, kind: str) -> int:
        self.scheduled.append((user_id, at, reason, kind))
        return len(self.scheduled)
