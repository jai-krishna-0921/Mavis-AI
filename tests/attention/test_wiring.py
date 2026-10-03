from types import SimpleNamespace

from mavis.agents import buttons, context_hooks
from mavis.attention import wiring as attention_wiring
from mavis.attention.wiring import get_digest, get_intake, get_pipeline, register_attention
from mavis.domain.events import EventType
from mavis.initiative import hooks, routines
from mavis.initiative import wiring as initiative_wiring
from mavis.initiative.handler import register
from mavis.initiative.wiring import build_initiative
from mavis.timers import system
from mavis.worker import runner


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


async def _no_items(user_id, start, end):
    return []


def _setup(recording_bus, fake_memory):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    initiative_wiring.set_current(init)
    register(init.handler)
    routines.register_brief_source(SimpleNamespace(name="inbox", items=_no_items))
    return init


async def test_register_attention_owns_email_and_plugs_registries(
    settings, recording_bus, fake_memory, embedder
):
    init = _setup(recording_bus, fake_memory)
    register_attention()
    register_attention()  # idempotent
    assert runner._event_handlers[EventType.EMAIL_RECEIVED] == [get_intake().on_email]
    completed = runner._event_handlers[EventType.TASK_COMPLETED]
    assert init.handler.handle in completed and get_intake().on_task_completed in completed
    assert "at:" in buttons.BUTTON_HANDLERS
    for kind in attention_wiring.SYSTEM_KINDS:
        assert kind.value in system.SYSTEM_WAKEUP_HANDLERS
    assert [s.name for s in routines.brief_sources()] == ["attention"]
    assert hooks.ENRICHERS.count(get_pipeline().enrich) == 1
    assert context_hooks.CONTEXT_PROVIDERS == [get_digest().context]


async def test_disabled_keeps_the_legacy_email_path(settings, recording_bus, fake_memory, monkeypatch):
    init = _setup(recording_bus, fake_memory)
    monkeypatch.setattr(settings, "attention_enabled", False)
    register_attention()
    assert runner._event_handlers[EventType.EMAIL_RECEIVED] == [init.handler.handle]
    assert [s.name for s in routines.brief_sources()] == ["inbox"]


def _snapshot() -> dict:
    """Every registry the worker wiring touches, as comparable values."""
    from mavis.agents import context_hooks as ch

    return {
        "events": {t: list(fns) for t, fns in runner._event_handlers.items() if fns},
        "jobs": dict(runner._job_handlers),
        "startup": list(runner._startup_hooks),
        "system": dict(system.SYSTEM_WAKEUP_HANDLERS),
        "buttons": dict(buttons.BUTTON_HANDLERS),
        "prefilters": list(hooks.PREFILTERS),
        "enrichers": list(hooks.ENRICHERS),
        "policies": list(hooks.DECISION_POLICIES),
        "brief": [s.name for s in routines.brief_sources()],
        "morning": list(routines._morning_hooks),
        "context": list(ch.CONTEXT_PROVIDERS),
    }


def _reset_registries() -> None:
    runner.clear_handlers()
    for hook_list in (hooks.PREFILTERS, hooks.ENRICHERS, hooks.DECISION_POLICIES):
        hook_list.clear()
    routines.clear_brief_sources()
    routines.clear_morning_hooks()
    buttons.BUTTON_HANDLERS.clear()
    system.SYSTEM_WAKEUP_HANDLERS.clear()
    context_hooks.clear_context_providers()


async def test_disabled_restores_the_pre_attention_wiring_exactly(settings, db, bus, memory, monkeypatch):
    """ATTENTION_ENABLED=false gives the same registries as a worker built without the attention layer."""
    from mavis.worker import handlers

    with monkeypatch.context() as m:  # only the stub is undone, not the settings fixture's patches
        m.setattr(handlers, "register_attention", lambda: None)
        handlers.register_default_handlers()
    legacy = _snapshot()
    _reset_registries()
    monkeypatch.setattr(settings, "attention_enabled", False)
    handlers.register_default_handlers()
    assert _snapshot() == legacy
    assert runner._event_handlers[EventType.EMAIL_RECEIVED] == [initiative_wiring.current().handler.handle]
    assert sorted(legacy["brief"]) == ["calendar", "inbox"]


async def test_default_handlers_wire_attention_without_duplicates(settings, db, bus, memory):
    from mavis.tools.integrations import wiring as integrations_wiring
    from mavis.worker.handlers import register_default_handlers

    register_default_handlers()
    first = _snapshot()
    register_default_handlers()  # idempotent: registers on every call, appends nothing twice
    assert _snapshot() == first
    assert runner._event_handlers[EventType.EMAIL_RECEIVED] == [get_intake().on_email]
    assert runner._event_handlers[EventType.BUTTON_PRESSED] == [buttons.dispatch_button]  # one dispatcher
    assert runner._event_handlers[EventType.TASK_COMPLETED] == [
        integrations_wiring.dispatch_task_completed,
        get_intake().on_task_completed,
    ]
    assert {"conn:", "at:"} <= set(buttons.BUTTON_HANDLERS)  # Phase 4 adds "ap:"
    # calendar stays, the live-search inbox source is replaced: no duplicate inbox lines
    assert [s.name for s in routines.brief_sources()] == ["calendar", "attention"]
    assert context_hooks.CONTEXT_PROVIDERS == [get_digest().context]


async def test_gmail_first_sync_message_stays_with_attention_on(settings, db, bus, memory, user, monkeypatch):
    """Spec 13: Phase 5's immediate first-sync message stays; the first look only adds to it when notable."""
    from datetime import UTC, datetime

    from mavis.domain.events import Event, Trust
    from mavis.tools.integrations import wiring as integrations_wiring
    from mavis.worker.handlers import register_default_handlers

    register_default_handlers()
    seen = []

    class Exec:
        async def notify(self, user, intent, untrusted=False, **kw):
            seen.append(intent.dedupe_key)
            return True

    monkeypatch.setattr(initiative_wiring, "_current", SimpleNamespace(executor=Exec()))
    event = Event(
        id=f"first_sync:{user.id}:gmail",
        user_id=user.id,
        type=EventType.TASK_COMPLETED,
        occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
        source="integrations",
        trust=Trust.UNTRUSTED,
        payload={"kind": "first_sync", "capability": "gmail", "noticed": ["x"]},
    )
    await integrations_wiring.dispatch_task_completed(event)
    assert seen == [f"first_sync:{user.id}:gmail"]


async def test_index_shares_memory_client_and_embedder(settings, db, bus, memory):
    from mavis.worker.handlers import register_default_handlers

    register_default_handlers()
    index = attention_wiring.get_index()
    assert index._client is memory.vector.client and index._embedder is memory.embedder
    assert attention_wiring._private_clients == []
    await attention_wiring.close_attention()  # never closes the shared client
    assert await memory.vector.client.collection_exists("episodes")


async def test_private_client_is_closed_on_shutdown(settings, recording_bus, fake_memory, embedder):
    _setup(recording_bus, fake_memory)
    register_attention()
    [client] = attention_wiring._private_clients
    await attention_wiring.close_attention()
    assert attention_wiring._private_clients == []


async def test_startup_smoke_sqlite_inprocess_bus(settings, db, bus, memory, user, clock, fake_llm):
    """Worker start on SQLite and the in-process bus: wiring, startup heal, one email end to end."""
    from datetime import UTC, datetime

    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import attention as repo
    from mavis.store.repo import users
    from mavis.timers.service import WakeupService
    from mavis.worker.handlers import register_default_handlers
    from tests.attention.helpers import email, running

    clock.set(datetime(2026, 10, 3, 6, 0, tzinfo=UTC))  # 11:30 IST
    await users.update_state(user.id, {"polling": {"gmail": True}})
    register_default_handlers()
    await runner.run_startup_hooks()
    wakeups = WakeupService()
    [evening] = await wakeups.pending(user.id, WakeupKind.SYSTEM_EVENING_WRAP)
    [retention] = await wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_RETENTION)
    assert evening.due_at == datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    assert retention.due_at == datetime(2026, 10, 3, 22, 0, tzinfo=UTC)

    spam = email(user.id, "smoke-1", subject="You won", labels=("SPAM",))
    async with running(bus):
        await bus.publish(spam)
    [row] = await repo.recent(user.id, datetime(2020, 1, 1, tzinfo=UTC))
    assert row.verdict == "dropped" and row.origin == repo.ORIGIN_LIVE
    assert fake_llm.structured_calls == [] and bus.dead_events == []
    await runner.run_startup_hooks()  # a restart re-arms nothing twice
    assert len(await wakeups.pending(user.id, WakeupKind.SYSTEM_EVENING_WRAP)) == 1
    assert len(await wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_RETENTION)) == 1


async def test_daily_chains_only_for_watched_users(settings, db, bus, memory, user, clock):
    from datetime import UTC, datetime

    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import attention as repo
    from mavis.timers.service import WakeupService
    from mavis.worker.handlers import register_default_handlers

    clock.set(datetime(2026, 10, 3, 6, 0, tzinfo=UTC))
    register_default_handlers()
    wakeups = WakeupService()
    await attention_wiring.heal_all()
    await attention_wiring.morning_maintenance(user.id)
    assert await wakeups.pending(user.id, WakeupKind.SYSTEM_EVENING_WRAP) == []
    assert await wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_RETENTION) == []
    # observations alone (Gmail since disconnected) still need retention
    await repo.insert_pending(
        user.id,
        "m1",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="x.in",
        sender_name="",
        received_at=clock.t,
        payload={},
    )
    await attention_wiring.morning_maintenance(user.id)
    assert len(await wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_RETENTION)) == 1
    assert len(await wakeups.pending(user.id, WakeupKind.SYSTEM_EVENING_WRAP)) == 1


async def test_morning_maintenance_steps_are_isolated(settings, db, bus, memory, user, clock, monkeypatch):
    from datetime import UTC, datetime

    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import users
    from mavis.timers.service import WakeupService
    from mavis.worker.handlers import register_default_handlers

    clock.set(datetime(2026, 10, 3, 6, 0, tzinfo=UTC))
    register_default_handlers()
    await users.update_state(user.id, {"polling": {"gmail": True}})

    async def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(attention_wiring.get_intake(), "heal", boom)
    monkeypatch.setattr(attention_wiring.get_evening(), "ensure", boom)
    await attention_wiring.morning_maintenance(user.id)  # never raises
    assert len(await WakeupService().pending(user.id, WakeupKind.SYSTEM_ATTENTION_RETENTION)) == 1
