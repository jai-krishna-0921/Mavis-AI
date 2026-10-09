# ruff: noqa: E501
from datetime import timedelta

import pytest

from mavis.domain.events import Event, EventType, JobKind, Trust
from mavis.domain.integrations import ConnectionState, PendingStatus
from mavis.domain.policy import Capability
from mavis.domain.wakeups import WakeupKind
from mavis.store.repo import connections
from mavis.tools.integrations.connect_flow import (
    CHECK_DELAYS,
    CHECK_KIND,
    PENDING_TTL,
    ConnectFlow,
)
from tests.tools.integrations.fakes import NOW


def make_flow(provider, cache, fake_bus, rec, state, on_active=None, clock=lambda: NOW):
    return ConnectFlow(provider=provider, cache=cache, bus=fake_bus, notify=rec.notify, schedule=rec.schedule,
                       state=state, base_url="https://mavis.test", on_active=on_active, clock=clock)


async def test_start_sends_url_button_and_schedules_checks(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="task-9")
    msg = rec.sent[-1]
    assert "work with your calendar" in msg.text and "Google" in msg.text
    assert msg.buttons[0][0].url == "https://connect.example/googlecalendar"
    assert msg.buttons[1][0].data == f"conn:no:{pid}"
    assert provider.links[0][2] == f"https://mavis.test/connect/callback?p={pid}"
    assert [(at - NOW, reason, kind) for _, at, reason, kind in rec.scheduled] == [
        (delay, str(pid), CHECK_KIND) for delay in CHECK_DELAYS
    ]
    assert [d // timedelta(minutes=1) for d in CHECK_DELAYS[:5]] == [1, 3, 10, 30, 60]
    assert CHECK_DELAYS[-1] > PENDING_TTL  # the 24h expiry is reachable


async def test_start_reuses_recent_pending_and_resends_link_for_user_commands(
    db, provider, cache, fake_bus, rec, state
):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    first = await flow.start(1, Capability.GMAIL, "")
    again = await flow.start(1, Capability.GMAIL, "")
    assert again == first and len(await connections.open_for(1, Capability.GMAIL)) == 1
    assert len(rec.sent) == 2
    assert rec.sent[-1].text.startswith("Here's your link again")
    assert rec.sent[-1].buttons[0][0].url == "https://connect.example/gmail"
    assert provider.links[-1][2].endswith(f"p={first}")


async def test_reused_pending_gets_checks_only_when_none_are_scheduled(
    db, provider, cache, fake_bus, rec, state
):
    async def no_checks(user_id, pending_id):
        return False

    flow = make_flow(provider, cache, fake_bus, rec, state)
    flow.has_checks = no_checks
    pid = await flow.start(1, Capability.GMAIL, "")
    rec.scheduled.clear()  # pretend the first batch never made it (crash after send)
    await flow.start(1, Capability.GMAIL, "")
    assert len(rec.scheduled) == len(CHECK_DELAYS) and {r for _, _, r, _ in rec.scheduled} == {str(pid)}

    async def has_checks(user_id, pending_id):
        return True

    flow.has_checks = has_checks
    rec.scheduled.clear()
    await flow.start(1, Capability.GMAIL, "")
    assert rec.scheduled == []


async def test_start_reuse_with_task_remembers_run_silently(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.start(1, Capability.GMAIL, "check and handle your email", task_id="a")
    await flow.start(1, Capability.GMAIL, "check and handle your email", task_id="b")
    assert len(rec.sent) == 1 and len(provider.links) == 1
    assert {p.task_id for p in await connections.open_for(1, Capability.GMAIL)} == {"a", "b"}


async def test_connect_when_provider_active_but_never_activated_runs_first_sync(
    db, provider, cache, fake_bus, rec, state
):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    activated = []

    async def on_active(user_id, capability):
        activated.append((user_id, capability))

    flow = make_flow(provider, cache, fake_bus, rec, state, on_active=on_active)
    await flow.start(1, Capability.GMAIL, "")
    assert [j.kind for j in fake_bus.jobs] == [JobKind.FIRST_SYNC] and activated == [(1, Capability.GMAIL)]
    assert "Connected" in rec.sent[-1].text and "already" not in rec.sent[-1].text
    # fully activated now (the activator would record polling): a second /connect just confirms
    await state.update(1, {"polling": {"gmail": True}})
    await flow.start(1, Capability.GMAIL, "")
    assert len(fake_bus.jobs) == 1 and rec.sent[-1].text == "Gmail is already connected ✓"


async def test_connections_command_reconciles_provider_state(db, provider, cache, fake_bus, rec, state):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.set_state(1, Capability.SLACK, ConnectionState.FAILED)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    text = await flow.status_text(1)
    assert "✅ Gmail: connected" in text and "Slack: needs reconnecting" in text
    assert [j.kind for j in fake_bus.jobs] == [JobKind.FIRST_SYNC]
    assert (await state.get(1))["synced"].get("gmail")


async def test_reconcile_resolves_pending_that_finished_unseen(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, Capability.GMAIL, "", task_id="t")
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    cache.invalidate(1)
    await flow.reconcile(1, Capability.GMAIL)
    assert (await connections.get_pending(pid)).status == PendingStatus.ACTIVE
    assert any(j.kind is JobKind.RESUME_TASK for j in fake_bus.jobs)


async def test_prompt_reconnect_is_deduped_per_capability_per_day(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.prompt_reconnect(1, Capability.GMAIL) is True
    assert rec.sent[-1].text.startswith("Your Gmail access has expired")
    assert rec.sent[-1].buttons[0][0].url
    assert await flow.prompt_reconnect(1, Capability.GMAIL) is False
    assert await flow.prompt_reconnect(1, Capability.CALENDAR) is True
    assert len(rec.sent) == 2
    later = NOW + timedelta(days=1, minutes=30)
    tomorrow = make_flow(provider, cache, fake_bus, rec, state, clock=lambda: later)
    assert await tomorrow.prompt_reconnect(1, Capability.GMAIL) is True
    # reconnecting clears the marker
    ev = Event(id="c9", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW, source="integrations",
               payload={"capability": "gmail", "state": "ACTIVE"})
    await flow.on_connection_changed(ev)
    assert "gmail" not in (await state.get(1)).get("reconnect_prompted", {})


async def test_start_when_already_active_resumes_task(db, provider, cache, fake_bus, rec, state):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.start(1, Capability.GMAIL, "", task_id="t") is None
    [resume] = [j for j in fake_bus.jobs if j.kind is JobKind.RESUME_TASK]
    assert resume.payload == {"task_id": "t", "value": {"connected": True}}
    assert rec.sent == []  # a resumed run is not announced


async def test_start_when_provider_unconfigured_tells_user(db, provider, cache, fake_bus, rec, state):
    provider.fail_link = True
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.start(1, Capability.GMAIL, "", task_id="t") is None
    assert "can't open a connection" in rec.sent[-1].text
    assert "COMPOSIO" not in rec.sent[-1].text
    assert fake_bus.jobs[0].payload["value"] == {"connected": False}


async def test_revoked_prompt_text(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.on_connect_interrupt("t", 1, {"type": "connect", "capability": "gmail",
                                            "reason": "access expired or was revoked", "revoked": True})
    assert rec.sent[-1].text.startswith("Your Gmail access has expired")


async def test_check_publishes_connection_changed_when_active(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, Capability.GMAIL, "")
    await flow.check(pid)
    assert fake_bus.events == []
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    await flow.on_check_wakeup(1, str(pid))
    [ev] = fake_bus.events
    assert ev.type is EventType.CONNECTION_CHANGED and ev.trust is Trust.SYSTEM
    assert ev.payload == {"capability": "gmail", "state": "ACTIVE", "pending_id": pid}


async def test_check_expires_old_pending(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, Capability.GMAIL, "")
    later = make_flow(provider, cache, fake_bus, rec, state, clock=lambda: NOW + timedelta(hours=25))
    await later.check(pid)
    assert (await connections.get_pending(pid)).status == PendingStatus.EXPIRED


async def test_first_sync_enqueued_once(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    ev = Event(id="c1", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW, source="integrations",
               payload={"capability": "gmail", "state": "ACTIVE"})
    await flow.on_connection_changed(ev)
    await flow.on_connection_changed(ev.model_copy(update={"id": "c2"}))
    assert len([j for j in fake_bus.jobs if j.kind is JobKind.FIRST_SYNC]) == 1
    assert "Connected ✓" in rec.sent[0].text


async def test_decline_resumes_false_and_records_not_now(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, Capability.GMAIL, "", task_id="t1")
    btn = Event(id="b1", user_id=1, type=EventType.BUTTON_PRESSED, occurred_at=NOW, source="telegram",
                payload={"data": f"conn:no:{pid}"}, trust=Trust.USER)
    await flow.on_button(btn, f"conn:no:{pid}")
    assert (await connections.get_pending(pid)).status == PendingStatus.DECLINED
    assert fake_bus.jobs[-1].payload == {"task_id": "t1", "value": {"connected": False}}
    assert (await state.get(1))["not_now"]["gmail"] == NOW.isoformat()


async def test_decline_ignores_other_users_pending(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, Capability.GMAIL, "", task_id="t1")
    await flow.decline(2, pid)
    assert (await connections.get_pending(pid)).status == PendingStatus.PENDING


async def test_failed_connection_offers_retry(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.start(1, Capability.SLACK, "", task_id="t2")
    ev = Event(id="f1", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW, source="integrations",
               payload={"capability": "slack", "state": "FAILED"})
    await flow.on_connection_changed(ev)
    assert rec.sent[-1].buttons[0][0].data == "conn:retry:slack"
    assert fake_bus.jobs[-1].payload == {"task_id": "t2", "value": {"connected": False}}


async def test_menu_status_and_disconnect(db, provider, cache, fake_bus, rec, state):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.offer_menu(1)
    labels = [row[0].label for row in rec.sent[-1].buttons]
    assert "Connect Gmail" not in labels and "Connect Slack" in labels
    text = await flow.status_text(1)
    assert "✅ Gmail" in text and "⚪ Slack" in text
    await flow.disconnect(1, Capability.GMAIL)
    assert provider.disconnected == [(1, "gmail")]
    assert "Disconnected Gmail" in rec.sent[-1].text


async def test_user_facing_copy_has_no_dashes(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    await flow.start(1, Capability.CALENDAR, "work with your calendar")
    await flow.start(1, Capability.GMAIL, "", revoked=True)
    provider.fail_link = True
    await flow.start(1, Capability.SLACK, "")
    await flow.offer_menu(1)
    await flow.disconnect(1, Capability.GMAIL)
    for cap, state_name in (("gmail", "ACTIVE"), ("slack", "FAILED")):
        ev = Event(id=f"d-{cap}", user_id=3, type=EventType.CONNECTION_CHANGED, occurred_at=NOW,
                   source="integrations", payload={"capability": cap, "state": state_name})
        await flow.on_connection_changed(ev)
    await flow.decline(1, 999)
    texts = [m.text for m in rec.sent] + [await flow.status_text(1)]
    texts += [b.label for m in rec.sent for row in m.buttons for b in row]
    assert texts
    assert not any("\u2014" in t or "\u2013" in t for t in texts)


async def test_disconnect_failure_hides_exception_text(db, provider, cache, fake_bus, rec, state):
    from mavis.domain.errors import IntegrationError

    async def boom(user, toolkit):
        raise IntegrationError("Composio 500 key=sk_secret")

    provider.disconnect = boom
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.disconnect(1, Capability.GMAIL)
    assert "sk_secret" not in rec.sent[-1].text and "Composio" not in rec.sent[-1].text
    assert "—" not in rec.sent[-1].text and "–" not in rec.sent[-1].text


async def test_connection_after_not_now_still_activates(db, provider, cache, fake_bus, rec, state, clock):
    """Wired like production (real check wakeups and cancel_checks): a sign-in finished after "Not now"
    is found by the prompt's scheduled check and activates."""
    from mavis.timers.service import WakeupService
    from mavis.tools.integrations.wiring import (
        cancel_connection_checks,
        connection_checks_pending,
        wakeup_schedule,
    )

    clock.set(NOW)
    activated = []

    async def on_active(user_id, cap):
        activated.append(cap)

    flow = ConnectFlow(provider=provider, cache=cache, bus=fake_bus, notify=rec.notify,
                       schedule=wakeup_schedule, state=state, base_url="https://mavis.test",
                       on_active=on_active, has_checks=connection_checks_pending,
                       cancel_checks=cancel_connection_checks, clock=lambda: NOW)
    pid = await flow.start(1, Capability.GMAIL, "")
    await flow.decline(1, pid)
    checks = await WakeupService().pending(1, WakeupKind.SYSTEM_CONNECTION_CHECK)
    assert checks and {w.reason for w in checks} == {str(pid)}  # "Not now" keeps the prompt's checks
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    await flow.on_check_wakeup(1, checks[0].reason)  # what the timer delivers for that wakeup
    [ev] = fake_bus.events
    await flow.on_connection_changed(ev)
    assert [j.kind for j in fake_bus.jobs] == [JobKind.FIRST_SYNC]
    assert activated == [Capability.GMAIL] and "Connected" in rec.sent[-1].text


async def test_disconnect_clears_synced(db, provider, cache, fake_bus, rec, state):
    await state.update(1, {"synced": {"gmail": "x", "slack": "y"}})
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.disconnect(1, Capability.GMAIL)
    assert (await state.get(1))["synced"] == {"slack": "y"}


async def test_one_connected_message_for_multiple_pendings(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await connections.create_pending(1, Capability.GMAIL, "", None, now=NOW)
    await connections.create_pending(1, Capability.GMAIL, "", None, now=NOW)
    ev = Event(id="m1", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW, source="integrations",
               payload={"capability": "gmail", "state": "ACTIVE"})
    await flow.on_connection_changed(ev)
    await flow.on_connection_changed(ev.model_copy(update={"id": "m2"}))
    assert len([m for m in rec.sent if "Connected" in m.text]) == 1


async def test_failed_event_without_waiting_pending_is_silent(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    ev = Event(id="s1", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW, source="integrations",
               payload={"capability": "slack", "state": "FAILED"})
    await flow.on_connection_changed(ev)
    assert rec.sent == []


async def test_check_keeps_waiting_when_reconnect_prompt_sees_old_failed_account(
    db, provider, cache, fake_bus, rec, state
):
    provider.set_state(1, Capability.GMAIL, ConnectionState.FAILED)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.prompt_reconnect(1, Capability.GMAIL) is True
    [pending] = await connections.open_for(1, Capability.GMAIL)
    sent = len(rec.sent)
    await flow.check(pending.id)
    assert fake_bus.events == [] and len(rec.sent) == sent
    assert (await connections.get_pending(pending.id)).status == PendingStatus.PENDING
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    await flow.check(pending.id)
    assert fake_bus.events[0].payload["state"] == "ACTIVE"


async def test_check_still_reports_failed_for_a_first_connect(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, Capability.GMAIL, "")
    provider.set_state(1, Capability.GMAIL, ConnectionState.FAILED)
    await flow.check(pid)
    assert fake_bus.events[0].payload["state"] == "FAILED"


async def test_prompt_reconnect_not_recorded_if_link_fails(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    provider.fail_link = True
    assert await flow.prompt_reconnect(1, Capability.GMAIL) is False
    assert "reconnect_prompted" not in await state.get(1)
    provider.fail_link = False
    assert await flow.prompt_reconnect(1, Capability.GMAIL) is True


async def test_concurrent_activation_enqueues_first_sync_once(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)

    class RacyState:  # both callers read "not synced" before either writes
        async def get(self, user_id):
            return {}

        async def update(self, user_id, patch):
            return patch

    flow.state = RacyState()
    await flow.reconcile(1, Capability.GMAIL)
    await flow.reconcile(1, Capability.GMAIL)
    assert len([j for j in fake_bus.jobs if j.kind is JobKind.FIRST_SYNC]) == 1


async def test_resolving_a_pending_cancels_its_checks(db, provider, cache, fake_bus, rec, state):
    cancelled = []

    async def cancel(user_id, pending_id):
        cancelled.append((user_id, pending_id))

    flow = make_flow(provider, cache, fake_bus, rec, state)
    flow.cancel_checks = cancel
    pid = await flow.start(1, Capability.GMAIL, "")
    ev = Event(id="c5", user_id=1, type=EventType.CONNECTION_CHANGED, occurred_at=NOW, source="integrations",
               payload={"capability": "gmail", "state": "ACTIVE"})
    await flow.on_connection_changed(ev)
    assert cancelled == [(1, pid)]


async def test_connection_required_interrupts_and_resumes(db, user, provider, cache, fake_bus, rec, state,
                                                          fake_llm, monkeypatch):
    """Index Review Focus #4 (folded P5 Task 8): missing Gmail pauses the task at connect_gate,
    ConnectFlow prompts, and the same run resumes and succeeds once the account is ACTIVE."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from mavis.agents import orchestrator_graph as og
    from mavis.domain.decisions import ComposedMessage
    from mavis.domain.integrations import ToolResult
    from mavis.domain.plans import Plan, PlanStep
    from mavis.domain.tasks import StepOutcome
    from mavis.store.repo import tasks
    from mavis.tools.integrations.actions import MailSearchArgs
    from mavis.tools.integrations.tools import gated
    from mavis.tools.registry import ToolContext

    provider.results["mail.search"] = ToolResult(ok=True, data={"messages": [{"subject": "Hi"}]})
    activated = []

    async def on_active(user_id, cap):
        activated.append((user_id, cap))

    flow = make_flow(provider, cache, fake_bus, rec, state, on_active=on_active)

    async def step(plan_step, user_id, context):
        ctx = ToolContext(user_id=user_id, timezone="Asia/Kolkata")
        return StepOutcome(ok=True, text=await gated(ctx, "mail.search", MailSearchArgs(), provider=provider,
                                                     cache=cache))

    monkeypatch.setattr(og, "run_step_agent", step)
    fake_llm.push_structured(Plan(goal="inbox", steps=[
        PlanStep(id="s1", agent="research", instruction="inbox")]))
    tid = await tasks.create(user.id, goal="anything new in my inbox?")
    graph = og.build_orchestrator().compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": f"task:{tid}"}}

    first = await graph.ainvoke(og.initial_state(await tasks.get(tid)), cfg)
    [intr] = first["__interrupt__"]
    assert intr.value["type"] == "connect" and intr.value["capability"] == "gmail"
    await flow.on_connect_interrupt(tid, user.id, intr.value)
    assert rec.sent[-1].buttons[0][0].url == "https://connect.example/gmail"
    assert "check and handle your email" in rec.sent[-1].text
    pending_id = int(provider.links[0][2].split("p=")[1])

    provider.set_state(user.id, Capability.GMAIL, ConnectionState.ACTIVE)  # user tapped and consented
    await flow.check(pending_id)
    [changed] = [e for e in fake_bus.events if e.type is EventType.CONNECTION_CHANGED]
    await flow.on_connection_changed(changed)

    resume = next(j for j in fake_bus.jobs if j.kind is JobKind.RESUME_TASK)
    assert resume.payload == {"task_id": str(tid), "value": {"connected": True}}
    assert [j.payload for j in fake_bus.jobs if j.kind is JobKind.FIRST_SYNC] == [{"capability": "gmail"}]
    assert activated == [(user.id, Capability.GMAIL)]
    assert (await connections.get_pending(pending_id)).status == PendingStatus.ACTIVE
    assert not any("Connected ✓" in m.text for m in rec.sent)  # the resumed task speaks instead

    # single-step plan: no critic call
    fake_llm.push_structured(ComposedMessage(send=True, messages=["One new email: Hi."]))
    final = await graph.ainvoke(Command(resume=resume.payload["value"]), cfg)
    assert final["results"]["s1"]["ok"] is True and "Hi" in final["results"]["s1"]["text"]


# --- hotfix3 RC4: one open connect prompt per user and capability ---------------------------------


async def test_second_task_an_hour_later_gets_the_link_again_not_expired(
    db, provider, cache, fake_bus, rec, state
):
    now = [NOW]
    flow = make_flow(provider, cache, fake_bus, rec, state, clock=lambda: now[0])
    first = await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="3")
    now[0] = NOW + timedelta(minutes=39)  # prod: 13:45 then 14:24 IST; the first link died after 10 min
    again = await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="6", revoked=True)
    assert again == first
    assert len(rec.sent) == 2 and rec.sent[-1].text.startswith("Here's your link again")
    assert "expired" not in rec.sent[-1].text
    assert {p.task_id for p in await connections.open_for(1, Capability.CALENDAR)} == {"3", "6"}


async def test_a_stale_open_prompt_gets_a_fresh_one(db, provider, cache, fake_bus, rec, state):
    now = [NOW]
    flow = make_flow(provider, cache, fake_bus, rec, state, clock=lambda: now[0])
    await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="3")
    now[0] = NOW + timedelta(hours=3)
    await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="9")
    assert len(rec.sent) == 2


async def test_joining_does_not_extend_the_prompt_window(db, provider, cache, fake_bus, rec, state):
    now = [NOW]
    flow = make_flow(provider, cache, fake_bus, rec, state, clock=lambda: now[0])
    first = await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="3")
    now[0] = NOW + timedelta(minutes=5)
    await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="6")  # joins silently
    assert len(rec.sent) == 1
    now[0] = NOW + timedelta(hours=2, minutes=5)  # 2h after the link went out, not after the join
    fresh = await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="9")
    assert fresh != first and len(rec.sent) == 2


# --- review round 2 (I3): never wait silently on a dead link; close every joined row --------------


async def test_dead_link_is_resent_once_then_joins_are_silent_again(
    db, provider, cache, fake_bus, rec, state
):
    now = [NOW]
    flow = make_flow(provider, cache, fake_bus, rec, state, clock=lambda: now[0])
    pid = await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="a")
    now[0] = NOW + timedelta(minutes=5)
    await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="b")
    assert len(rec.sent) == 1  # the link is still alive
    now[0] = NOW + timedelta(minutes=30)
    await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="c")
    assert len(rec.sent) == 2 and rec.sent[-1].text.startswith("Here's your link again")
    assert provider.links[-1][2].endswith(f"p={pid}")
    now[0] = NOW + timedelta(minutes=35)
    await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="d")
    assert len(rec.sent) == 2  # the re-sent link is alive


async def _three_waiting(flow, now) -> int:
    pid = await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="a")
    now[0] = NOW + timedelta(minutes=5)
    await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="b")
    await flow.start(1, Capability.CALENDAR, "work with your calendar", task_id="c")
    return pid


async def test_decline_closes_and_resumes_every_waiting_task(db, provider, cache, fake_bus, rec, state):
    now = [NOW]
    flow = make_flow(provider, cache, fake_bus, rec, state, clock=lambda: now[0])
    pid = await _three_waiting(flow, now)
    await flow.decline(1, pid)
    assert await connections.open_for(1, Capability.CALENDAR) == []
    resumed = {j.payload["task_id"] for j in fake_bus.jobs if j.kind is JobKind.RESUME_TASK}
    assert resumed == {"a", "b", "c"}
    resumes = [j for j in fake_bus.jobs if j.kind is JobKind.RESUME_TASK]
    assert all(j.payload["value"] == {"connected": False} for j in resumes)


async def test_expiry_closes_and_resumes_every_waiting_task(db, provider, cache, fake_bus, rec, state):
    now = [NOW]
    flow = make_flow(provider, cache, fake_bus, rec, state, clock=lambda: now[0])
    pid = await _three_waiting(flow, now)
    now[0] = NOW + PENDING_TTL + timedelta(minutes=1)
    await flow.check(pid)
    assert await connections.open_for(1, Capability.CALENDAR) == []
    resumed = {j.payload["task_id"] for j in fake_bus.jobs if j.kind is JobKind.RESUME_TASK}
    assert resumed == {"a", "b", "c"}


@pytest.mark.parametrize(("capability", "prefix"), [
    (Capability.GMAIL, "gmail:"), (Capability.SLACK, "slack:"),
])
async def test_disconnect_forgets_what_was_learned_from_that_source(
        db, provider, cache, fake_bus, rec, state, capability, prefix):
    forgotten = []

    async def forget(user_id, p):
        forgotten.append((user_id, p))
        return 3

    provider.set_state(1, capability, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    flow.forget_source = forget
    await flow.disconnect(1, capability)
    assert forgotten == [(1, prefix)]
    assert "removed what I had learned" in rec.sent[-1].text
    assert not any(c in rec.sent[-1].text for c in "—–")


async def test_a_failing_forget_does_not_undo_the_disconnect(db, provider, cache, fake_bus, rec, state):
    async def boom(user_id, p):
        raise RuntimeError("down")

    provider.set_state(1, Capability.SLACK, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    flow.forget_source = boom
    await flow.disconnect(1, Capability.SLACK)
    assert provider.disconnected == [(1, "slack")] and "Disconnected Slack" in rec.sent[-1].text


async def test_more_access_offer_has_a_reconnect_button_and_is_sent_once_a_day(
    db, provider, cache, fake_bus, rec, state
):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.offer_more_access(1, Capability.GMAIL) is True
    msg = rec.sent[-1]
    assert msg.text.startswith("That needs more access to your Gmail than you allowed.")
    assert msg.buttons[0][0].label == "Reconnect Gmail" and msg.buttons[0][0].url
    assert await flow.offer_more_access(1, Capability.GMAIL) is False  # not again today
    assert len(rec.sent) == 1
    assert await flow.offer_more_access(2, Capability.GMAIL) is True  # someone else is a different story
    assert await connections.open_for(1, Capability.GMAIL) == []  # no pending: nothing "connects" until they sign in


async def test_menu_and_status_list_only_what_the_provider_can_connect(db, provider, cache, fake_bus, rec, state):
    provider.can_connect = lambda c: c is not Capability.NOTION
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.offer_menu(1)
    labels = [b.label for row in rec.sent[-1].buttons for b in row]
    assert labels and not any("Notion" in label for label in labels)
    assert "Notion" not in await flow.status_text(1)
