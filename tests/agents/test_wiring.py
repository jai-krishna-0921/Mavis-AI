"""Phase 4 worker wiring: one handler per event type, task jobs, approval buttons, wakeups, interrupts."""

from __future__ import annotations

import pytest

from mavis.agents import buttons, conversation, interrupts, wiring
from mavis.attention.wiring import get_intake
from mavis.domain.errors import ConnectionRequired, IntegrationError
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.domain.integrations import ConnectionState
from mavis.domain.messages import TAINT_SUFFIX, Role
from mavis.domain.policy import Capability
from mavis.domain.tasks import TaskOrigin, TaskStatus
from mavis.initiative import routines, task_delivery
from mavis.policy import approvals as approval_flow
from mavis.store.db import utcnow
from mavis.store.repo import messages, tasks
from mavis.timers import system
from mavis.tools import registry as registry_mod
from mavis.tools.integrations import wiring as integrations_wiring
from mavis.worker import runner
from mavis.worker.handlers import register_default_handlers


def _getter(value):
    def getter():
        return value

    getter.cache_clear = lambda: None  # the autouse reset fixture clears these singletons
    return getter


@pytest.fixture
def integ(monkeypatch, provider, cache):
    from mavis.tools import integrations

    monkeypatch.setattr(integrations, "get_provider", _getter(provider))
    monkeypatch.setattr(integrations, "get_connection_cache", _getter(cache))
    return provider


def _wakeup(user_id: int, kind: str, reason: str) -> Event:
    return Event(id=f"wk:{kind}:{reason}", user_id=user_id, type=EventType.WAKEUP, occurred_at=utcnow(),
                 source="timer", payload={"kind": kind, "reason": reason, "wakeup_id": 1}, trust=Trust.SYSTEM)


def test_register_installs_handlers(settings):
    register_default_handlers()
    register_default_handlers()  # idempotent: nothing is registered twice
    events = runner._event_handlers
    assert events[EventType.USER_MESSAGE] == [conversation.run_turn]
    # attention is on by default and appends its own TASK_COMPLETED handler (first_sync only)
    assert events[EventType.TASK_COMPLETED] == [integrations_wiring.dispatch_task_completed,
                                                get_intake().on_task_completed]
    assert events[EventType.BUTTON_PRESSED] == [buttons.dispatch_button]
    assert events[EventType.TASK_PROGRESS] == [task_delivery.on_progress]
    assert {"ap:", "conn:", "at:"} <= set(buttons.BUTTON_HANDLERS)
    assert {JobKind.RUN_TASK, JobKind.RESUME_TASK, JobKind.LEARN, JobKind.CONNECTION_CHECK} <= set(
        runner._job_handlers)
    assert system.SYSTEM_WAKEUP_HANDLERS["system_approval_remind"] is approval_flow.on_remind_wakeup
    assert system.SYSTEM_WAKEUP_HANDLERS["system_approval_expire"] is approval_flow.on_expire_wakeup
    assert "system_task_delivery" in system.SYSTEM_WAKEUP_HANDLERS
    flow = integrations_wiring.get_connect_flow()
    assert interrupts.INTERRUPT_HANDLERS["connect"] == flow.on_connect_interrupt
    assert interrupts.INTERRUPT_HANDLERS["approval"] is interrupts._approval
    # sweeps hooked once each, however often the worker wiring runs
    assert runner._startup_hooks.count(approval_flow.sweep_approvals) == 1
    assert routines._morning_hooks.count(approval_flow.sweep_for_user) == 1
    reg = registry_mod.get_registry()
    assert reg.capability_check is integrations_wiring.capability_check
    assert reg.capability_reason is integrations_wiring.capability_reason
    assert reg.available is integrations_wiring.tool_available


def test_interrupt_defaults_are_restored_between_tests():
    """The autouse reset put the Phase 4 default back after the previous test wired ConnectFlow."""
    assert interrupts.INTERRUPT_HANDLERS["connect"] is interrupts._connect_unavailable
    assert registry_mod._REGISTRY is None


async def test_system_wakeups_dispatch_approval_and_delivery_kinds(user, monkeypatch):
    seen: list = []

    async def _remind(user_id, aid):
        seen.append(("remind", aid))

    async def _expire(user_id, aid):
        seen.append(("expire", aid))

    async def _redeliver(user_id, tid):
        seen.append(("redeliver", tid))

    monkeypatch.setattr(approval_flow, "remind", _remind)
    monkeypatch.setattr(approval_flow, "expire", _expire)
    monkeypatch.setattr(task_delivery, "redeliver", _redeliver)
    wiring.register()
    assert await system.dispatch_system_wakeup(_wakeup(user.id, "system_approval_remind", "approval:4"))
    assert await system.dispatch_system_wakeup(_wakeup(user.id, "system_approval_expire", "approval:4"))
    assert await system.dispatch_system_wakeup(_wakeup(user.id, "system_task_delivery", "task:9"))
    assert await system.dispatch_system_wakeup(_wakeup(user.id, "system_task_delivery", "task:x"))
    assert not await system.dispatch_system_wakeup(_wakeup(user.id, "agent", "morning check-in"))
    assert seen == [("remind", 4), ("expire", 4), ("redeliver", 9)]


async def test_job_handlers_call_the_task_runner(monkeypatch):
    calls: list = []

    async def _run(task_id):
        calls.append(("run", task_id))

    async def _resume(task_id, value):
        calls.append(("resume", task_id, value))

    from mavis.agents import orchestrator

    monkeypatch.setattr(orchestrator, "run_task", _run)
    monkeypatch.setattr(orchestrator, "resume_task", _resume)
    wiring.register()
    jobs = runner._job_handlers
    await jobs[JobKind.RUN_TASK](Job(id="j1", user_id=1, kind=JobKind.RUN_TASK, payload={"task_id": 3}))
    await jobs[JobKind.RESUME_TASK](Job(id="j2", user_id=1, kind=JobKind.RESUME_TASK, payload={
        "task_id": 3, "approval_id": 5, "decision": "ok", "instructions": ""}))
    await jobs[JobKind.RESUME_TASK](Job(id="j3", user_id=1, kind=JobKind.RESUME_TASK, payload={
        "task_id": "3", "value": {"connected": True}}))  # ConnectFlow's shape: task_id as a str
    assert calls == [
        ("run", 3),
        ("resume", 3, {"approval_id": 5, "decision": "ok", "instructions": ""}),
        ("resume", 3, {"connected": True}),
    ]


async def test_button_dispatch_by_prefix():
    seen = []

    async def h(event, data):
        seen.append(data)

    buttons.register_button_handler("x:", h)
    ev = Event(id="b1", user_id=1, type=EventType.BUTTON_PRESSED, occurred_at=utcnow(), source="telegram",
               payload={"data": "x:42"}, trust=Trust.USER)
    assert await buttons.dispatch_button(ev) is True
    assert await buttons.dispatch_button(ev.model_copy(update={"payload": {"data": "zz"}})) is False
    assert seen == ["x:42"]


# --- capability check (F18) -----------------------------------------------------------------------


async def test_capability_check_follows_the_connection_state(user, integ, cache):
    check = integrations_wiring.capability_check
    assert await check(user.id, Capability.WEB) is True
    assert await check(user.id, Capability.SANDBOX) is True
    assert await check(user.id, Capability.GMAIL) is False
    integ.set_state(user.id, Capability.CALENDAR, ConnectionState.ACTIVE)
    cache.invalidate(user.id)  # statuses are cached briefly
    assert await check(user.id, Capability.CALENDAR) is True
    integ.set_state(user.id, Capability.SLACK, ConnectionState.FAILED)
    cache.invalidate(user.id)
    with pytest.raises(ConnectionRequired) as err:
        await check(user.id, Capability.SLACK)
    assert err.value.revoked is True and err.value.reason == "work with your Slack"


async def test_capability_check_passes_when_the_provider_is_down(user, integ, monkeypatch):
    async def down(_user):
        raise IntegrationError("Composio answered 503")

    monkeypatch.setattr(integ, "status", down)
    assert await integrations_wiring.capability_check(user.id, Capability.GMAIL) is True


async def test_connect_comes_before_approval_with_readable_reason(user, integ, settings):
    """An outward tool on an unconnected account asks to connect first, in plain words."""
    from mavis.tools.integrations.actions import MailComposeArgs

    register_default_handlers()
    reg = registry_mod.get_registry()
    tool = reg.get("mail_send")
    with pytest.raises(ConnectionRequired) as err:
        await reg.invoke(tool, user.id, MailComposeArgs(to=["j@example.com"], subject="Hi", body="Late"))
    assert err.value.capability is Capability.GMAIL
    assert err.value.reason == "check and handle your email"


def test_integration_tools_hidden_without_a_configured_provider(settings, integ):
    register_default_handlers()
    reg = registry_mod.get_registry()
    assert "mail_search" in {t.name for t in reg.for_agent("conversation", 1)}
    integ.configured = False
    names = {t.name for t in reg.for_agent("conversation", 1)}
    assert "mail_search" not in names and "web_search" in names


# --- delivery taint marker ------------------------------------------------------------------------


def _completed(task_id: int, user_id: int, tainted: bool) -> Event:
    return Event(id=f"task:{task_id}:completed", user_id=user_id, type=EventType.TASK_COMPLETED,
                 occurred_at=utcnow(), source="agent", trust=Trust.SYSTEM,
                 payload={"task_id": task_id, "messages": ["Found it.", "Two options."], "artifacts": [],
                          "origin": TaskOrigin.USER, "notify_on_complete": True, "tainted": tainted})


async def test_tainted_task_result_is_logged_with_the_taint_marker(user):
    tid = await tasks.create(user.id, goal="summarize my inbox", tainted=True)
    await task_delivery.deliver_task_result(_completed(tid, user.id, tainted=True))
    await task_delivery.deliver_task_result(_completed(tid, user.id, tainted=True))  # redelivered event
    log = [m for m in await messages.recent(user.id) if m.role == Role.ASSISTANT.value]
    assert [m.event_id for m in log] == [f"task:{tid}:m0{TAINT_SUFFIX}", f"task:{tid}:m1{TAINT_SUFFIX}"]


async def test_clean_task_result_is_logged_without_the_marker(user):
    tid = await tasks.create(user.id, goal="research PLM tools")
    await task_delivery.deliver_task_result(_completed(tid, user.id, tainted=False))
    log = [m for m in await messages.recent(user.id) if m.role == Role.ASSISTANT.value]
    assert [m.event_id for m in log] == [f"task:{tid}:m0", f"task:{tid}:m1"]
    assert not any(m.event_id.endswith(TAINT_SUFFIX) for m in log)


async def test_redelivered_tainted_result_keeps_the_marker(user, monkeypatch):
    class _Policy:
        async def record(self, *a, **k):
            return None

    monkeypatch.setattr(task_delivery.pings, "PingPolicy", _Policy)
    tid = await tasks.create(user.id, goal="g", origin=TaskOrigin.INITIATIVE, tainted=True)
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.DONE, result_text="Heads up on that email.")
    await task_delivery.redeliver(user.id, tid)
    [m] = [m for m in await messages.recent(user.id) if m.role == Role.ASSISTANT.value]
    assert m.event_id == f"task:{tid}:m0{TAINT_SUFFIX}"
