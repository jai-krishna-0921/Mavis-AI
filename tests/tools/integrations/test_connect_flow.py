from datetime import timedelta

from mavis.domain.events import Event, EventType, JobKind, Trust
from mavis.domain.integrations import ConnectionState, PendingStatus
from mavis.domain.policy import Capability
from mavis.store.repo import connections
from mavis.tools.integrations.connect_flow import CHECK_KIND, ConnectFlow
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
        (timedelta(minutes=m), str(pid), CHECK_KIND) for m in (1, 3, 10)
    ]


async def test_start_reuses_recent_link_without_resending(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.start(1, Capability.GMAIL, "check and handle your email", task_id="a")
    await flow.start(1, Capability.GMAIL, "check and handle your email", task_id="b")
    assert len(rec.sent) == 1 and len(provider.links) == 1
    assert {p.task_id for p in await connections.open_for(1, Capability.GMAIL)} == {"a", "b"}


async def test_start_when_already_active_resumes_task(db, provider, cache, fake_bus, rec, state):
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.start(1, Capability.GMAIL, "", task_id="t") is None
    assert fake_bus.jobs[0].kind is JobKind.RESUME_TASK
    assert fake_bus.jobs[0].payload == {"task_id": "t", "value": {"connected": True}}


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


async def test_connection_after_not_now_still_activates(db, provider, cache, fake_bus, rec, state):
    flow = make_flow(provider, cache, fake_bus, rec, state)
    pid = await flow.start(1, Capability.GMAIL, "")
    await flow.decline(1, pid)
    provider.set_state(1, Capability.GMAIL, ConnectionState.ACTIVE)
    activated = []

    async def on_active(user_id, cap):
        activated.append(cap)

    flow.on_active = on_active
    await flow.check(pid)
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
