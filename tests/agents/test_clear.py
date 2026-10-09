"""/clear: deleting recent Telegram messages, clearing history, the full reset, cancel and the rate limit."""

from __future__ import annotations

import functools
from datetime import timedelta

import pytest

from mavis.access import commands, deletion
from mavis.agents import clear
from mavis.agents.buttons import dispatch_button
from mavis.channels.base import ChannelRateLimited
from mavis.channels.outbox_sender import deliver_pending
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.memory import Entity
from mavis.domain.messages import Role
from mavis.domain.tasks import ApprovalStatus, TaskStatus
from mavis.store.db import utcnow
from mavis.store.repo import approvals, chat_ids, messages, outbox, tasks, users
from tests.tools.integrations.native.conftest import *  # noqa: F403 - fixtures

CHAT = 7001


@pytest.fixture(autouse=True)
def _wired(provider, monkeypatch, memory_checkpointer, channel):
    channel.record_ids = True  # like TelegramChannel, note the ids of what is sent
    monkeypatch.setattr("mavis.tools.integrations.get_provider", functools.cache(lambda: provider))
    deletion.register()
    clear.register()


@pytest.fixture
def bus(recording_bus):
    """Jobs are recorded, not run: a test runs the DELETE_USER job itself with deletion.run_deletion."""
    from mavis.bus import set_bus

    set_bus(recording_bus)
    yield recording_bus
    set_bus(None)


@pytest.fixture
def no_fallback(monkeypatch):
    """Only the ids Mavis noted are tried (no sliding range), so the 48h rule is observable on its own."""
    from mavis.config import get_settings

    monkeypatch.setenv("CLEAR_FALLBACK_SPAN", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _user(chat: int = CHAT, tier: str = "standard", status: str = "active") -> int:
    u, _ = await users.get_or_create_by_chat(chat, "Priya", telegram_user_id=chat)
    await users.update(u.id, status=status, tier=tier, composio_user_id=f"mavis-test-{u.id}")
    return u.id


def _press(uid: int, data: str, eid: str, source: str = "telegram", message_id: int = 900) -> Event:
    return Event(id=eid, user_id=uid, type=EventType.BUTTON_PRESSED, occurred_at=utcnow(), source=source,
                 payload={"data": data, "message_id": message_id}, trust=Trust.USER)


def _command(uid: int, eid: str, source: str = "telegram", message_id: int = 800) -> Event:
    return Event(id=eid, user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source=source,
                 payload={"text": "/clear", "command": "clear", "message_id": message_id}, trust=Trust.USER)


async def _ask(uid: int, channel, eid: str = "c:1") -> list[str]:
    assert await commands.command_gate(_command(uid, eid)) is False
    await deliver_pending(channel)
    return channel.texts


async def _seed_history(uid: int) -> None:
    for i in range(3):
        await messages.log(uid, Role.USER, f"message {i}", event_id=f"h:{uid}:{i}")


# --- deleting messages ----------------------------------------------------------------------------------


async def test_sweep_deletes_noted_ids_in_batches_of_100(db, channel, no_fallback):
    now = utcnow()
    await chat_ids.record(CHAT, range(1, 251), "out", now - timedelta(hours=2))
    report = await clear.delete_recent_messages(CHAT, now=now)
    assert [len(ids) for _, ids in channel.deleted] == [100, 100, 50]
    assert report.batches == 3 and report.refused == 0
    assert sorted(i for _, ids in channel.deleted for i in ids) == list(range(1, 251))
    assert await chat_ids.since(CHAT, now - timedelta(days=3)) == []  # forgotten once deleted


@pytest.mark.parametrize("hours,deleted", [(47.5, False), (46.0, True), (1.0, True), (49.0, False)])
async def test_the_48_hour_boundary(db, channel, no_fallback, hours, deleted):
    """Telegram refuses messages 48h old or more; Mavis stops an hour short and never tries older ids."""
    now = utcnow()
    await chat_ids.record(CHAT, [55], "in", now - timedelta(hours=hours))
    await chat_ids.record(CHAT, [56], "out", now)  # something fresh, so a sweep always happens
    await clear.delete_recent_messages(CHAT, now=now)
    sent = [i for _, ids in channel.deleted for i in ids]
    assert (55 in sent) is deleted and 56 in sent


async def test_partial_failure_keeps_going_and_keeps_the_refused_ids(db, channel, no_fallback):
    now = utcnow()
    await chat_ids.record(CHAT, range(1, 251), "out", now)
    channel.delete_results = [True, False, True]  # the middle batch (ids 51..150) is refused
    report = await clear.delete_recent_messages(CHAT, now=now)
    assert report.refused == 1 and not report.stopped_early
    left = await chat_ids.since(CHAT, now - timedelta(hours=1))
    assert sorted(left) == list(range(51, 151))


async def test_repeated_failures_stop_the_sweep_without_an_error(db, channel, no_fallback):
    now = utcnow()
    await chat_ids.record(CHAT, range(1, 501), "out", now)
    channel.delete_results = [RuntimeError("boom"), False, True, True, True]
    report = await clear.delete_recent_messages(CHAT, now=now)
    assert report.stopped_early and report.batches == 2 and channel.deleted == []


async def test_a_rate_limit_is_waited_out_once(db, channel, no_fallback, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(clear.asyncio, "sleep", fake_sleep)
    await chat_ids.record(CHAT, [1, 2], "out", utcnow())
    channel.delete_results = [ChannelRateLimited(3.0), True]
    report = await clear.delete_recent_messages(CHAT)
    assert slept == [3.0] and report.refused == 0 and channel.deleted == [(CHAT, [2, 1])]


async def test_unnoted_messages_fall_back_to_a_range_below_the_newest_known_id(db, channel, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("CLEAR_FALLBACK_SPAN", "150")
    get_settings.cache_clear()
    await chat_ids.record(CHAT, [200], "in", utcnow())
    await clear.delete_recent_messages(CHAT)
    sent = [i for _, ids in channel.deleted for i in ids]
    assert sent[0] == 200 and sent[1] == 199 and len(sent) == 150 and 51 in sent and 50 not in sent
    assert all(len(ids) <= 100 for _, ids in channel.deleted)


async def test_the_message_ids_of_both_sides_are_noted(db, channel):
    uid = await _user()
    ids = await channel.send_text(CHAT, "hello")
    from mavis.agents.clear import _note_inbound

    ev = Event(id="in:1", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source="telegram",
               payload={"text": "hi", "message_id": 4242}, trust=Trust.USER)
    assert await _note_inbound(ev) is True and await _note_inbound(ev) is True  # idempotent on retry
    noted = await chat_ids.since(CHAT, utcnow() - timedelta(hours=1))
    assert sorted(noted) == sorted([*ids, 4242])
    slack = ev.model_copy(update={"id": "in:2", "source": "slack_chat", "payload": {"message_id": 99}})
    await _note_inbound(slack)
    assert 99 not in await chat_ids.since(CHAT, utcnow() - timedelta(hours=1))


# --- option 1 -------------------------------------------------------------------------------------------


async def test_clear_command_offers_three_choices_without_dashes(db, channel):
    uid = await _user()
    out = await _ask(uid, channel)
    assert out == [clear.MENU_TEXT]
    labels = [b.label for row in channel.sent[-1].buttons for b in row]
    assert labels == ["Clear this chat", "Start fresh (forget everything)", "Cancel"]
    assert all("—" not in t and "–" not in t for t in [clear.MENU_TEXT, clear.FULL_WARNING,
                                                                   clear.CLEARED_TEXT, clear.GREETING])


async def test_clear_chat_clears_history_and_deletes_messages_but_keeps_memory(db, channel, memory):
    uid = await _user()
    await _seed_history(uid)
    await memory.graph.upsert_entity(uid, Entity(name="Mira", label="Person"))
    await users.update(uid, name="Priya")
    await _ask(uid, channel)
    await chat_ids.record(CHAT, [10, 11, 12], "out", utcnow())
    await dispatch_button(_press(uid, "clr:chat", "b:1"))
    await deliver_pending(channel)
    assert await messages.recent(uid, 10) == []
    assert [e.name for e in await memory.graph.entities(uid)] == ["Mira"]  # memory is kept
    assert (await users.get(uid)).status == "active" and (await users.get(uid)).name == "Priya"
    sent = {i for _, ids in channel.deleted for i in ids}
    assert {10, 11, 12} <= sent
    assert channel.texts[-2:] == [clear.CLEARED_TEXT, clear.GREETING]
    assert "Clear history" in clear.CLEARED_TEXT and "memories" in clear.CLEARED_TEXT


async def test_clear_chat_cancels_running_tasks_and_open_approvals(db, channel):
    uid = await _user()
    task_id = await tasks.create(uid, "research flights")
    await tasks.set_status(task_id, TaskStatus.RUNNING)
    aid = await approvals.create(uid, None, "send_email", {"to": "a@b.c"}, "Send it?",
                                 utcnow() + timedelta(hours=1))
    await _ask(uid, channel)
    await dispatch_button(_press(uid, "clr:chat", "b:2"))
    assert (await tasks.get(task_id)).status == TaskStatus.CANCELLED.value
    assert (await approvals.get(aid)).status == ApprovalStatus.REJECTED.value


async def test_clear_chat_drops_unsent_messages_but_sends_the_fresh_greeting(db, channel):
    uid = await _user()
    await outbox.enqueue_now(clear.Outbound(user_id=uid, text="stale reply", dedupe_key="stale:1"))
    await _ask(uid, channel)
    await outbox.enqueue_now(clear.Outbound(user_id=uid, text="another stale reply", dedupe_key="stale:2"))
    await dispatch_button(_press(uid, "clr:chat", "b:3"))
    await deliver_pending(channel)
    assert "another stale reply" not in channel.texts and channel.texts[-1] == clear.GREETING


async def test_a_menu_tap_works_once_and_expires(db, channel, clock):
    uid = await _user()
    await _ask(uid, channel)
    await dispatch_button(_press(uid, "clr:chat", "b:4"))
    await dispatch_button(_press(uid, "clr:chat", "b:5"))  # a second tap on the same menu
    await _ask(uid, channel, "c:2")
    clock.advance(minutes=11)
    await dispatch_button(_press(uid, "clr:chat", "b:6"))
    await deliver_pending(channel)
    assert channel.texts.count(clear.STALE_TEXT) == 2 and channel.texts.count(clear.GREETING) == 1


async def test_cancel_changes_nothing(db, channel):
    uid = await _user()
    await _seed_history(uid)
    await _ask(uid, channel)
    await dispatch_button(_press(uid, "clr:no", "b:7"))
    await dispatch_button(_press(uid, "clr:chat", "b:8"))  # the menu is gone after Cancel
    await deliver_pending(channel)
    assert len(await messages.recent(uid, 10)) == 3 and channel.deleted == []
    assert clear.CANCELLED_TEXT in channel.texts and clear.STALE_TEXT in channel.texts


async def test_clear_is_rate_limited_per_user(db, channel, settings):
    a, b = await _user(7001), await _user(7002)
    for i in range(settings.clear_max_per_hour):
        assert (await _ask(a, channel, f"a:{i}"))[-1] == clear.MENU_TEXT
        channel.sent.clear()
    await _ask(a, channel, "a:over")
    assert channel.texts == [clear.TOO_OFTEN_TEXT]
    channel.sent.clear()
    await _ask(b, channel, "b:first")  # another user is unaffected
    assert channel.texts == [clear.MENU_TEXT]


async def test_a_retried_ask_is_counted_once(db, channel, settings):
    uid = await _user()
    for _ in range(settings.clear_max_per_hour + 2):
        await _ask(uid, channel, "same-event")
    assert clear.TOO_OFTEN_TEXT not in channel.texts


async def test_the_limit_lifts_after_an_hour(db, channel, clock, settings):
    uid = await _user()
    for i in range(settings.clear_max_per_hour):
        await _ask(uid, channel, f"e:{i}")
    clock.advance(minutes=61)
    channel.sent.clear()
    assert (await _ask(uid, channel, "e:later")) == [clear.MENU_TEXT]


async def test_slack_gets_a_telegram_only_reply(db, channel):
    uid = await _user()
    assert await commands.command_gate(_command(uid, "s:1", source="slack_chat")) is False
    await deliver_pending(channel)
    assert channel.texts == [clear.TELEGRAM_ONLY_TEXT] and channel.deleted == []
    await dispatch_button(_press(uid, "clr:chat", "s:2", source="slack_chat"))
    assert channel.deleted == []


async def test_settings_offers_clear(db, channel):
    from mavis.agents import settings_flow

    uid = await _user()
    ev = _command(uid, "set:1")
    await settings_flow.settings_command(ev, await users.get(uid), [])
    await deliver_pending(channel)
    data = [b.data for row in channel.sent[-1].buttons for b in row]
    assert "clr:ask" in data
    await dispatch_button(_press(uid, "clr:ask", "set:2"))
    await deliver_pending(channel)
    assert channel.texts[-1] == clear.MENU_TEXT


# --- option 2 -------------------------------------------------------------------------------------------


async def _full_reset(uid: int, channel, bus) -> None:
    await _ask(uid, channel)
    await dispatch_button(_press(uid, "clr:full", "f:1"))
    await deliver_pending(channel)
    assert channel.texts[-1] == clear.FULL_WARNING
    assert [b.label for row in channel.sent[-1].buttons for b in row] == ["Yes, forget everything", "Cancel"]
    await dispatch_button(_press(uid, "clr:yes", "f:2"))


async def test_the_warning_comes_before_anything_is_erased(db, channel, bus):
    uid = await _user()
    await _seed_history(uid)
    await _ask(uid, channel)
    await dispatch_button(_press(uid, "clr:full", "f:0"))
    assert (await users.get(uid)).status == "active" and len(await messages.recent(uid, 10)) == 3
    await dispatch_button(_press(uid, "clr:no", "f:00"))
    await dispatch_button(_press(uid, "clr:yes", "f:000"))  # confirmation after Cancel does nothing
    assert (await users.get(uid)).status == "active"


async def test_full_reset_readmits_an_invited_user_without_an_invite(db, channel, bus, memory, invite_mode):
    uid = await _user(tier="trusted")
    await _seed_history(uid)
    await memory.graph.upsert_entity(uid, Entity(name="Mira", label="Person"))
    await chat_ids.record(CHAT, [30, 31], "out", utcnow())
    await _full_reset(uid, channel, bus)
    me = await users.get(uid)
    assert me.status == "deleting"  # the gate drops events until the job has finished
    assert bus.jobs[-1].payload == {"reset": True}
    await deletion.run_deletion(bus.jobs[-1])
    me = await users.get(uid)
    assert (me.status, me.tier, me.telegram_chat_id, me.name) == ("active", "trusted", CHAT, None)
    assert me.deleted_at is None and me.composio_user_id == f"mavis-dev-{uid}"
    assert await messages.recent(uid, 10) == [] and await memory.graph.entities(uid) == []
    assert {30, 31} <= {i for _, ids in channel.deleted for i in ids}
    await deliver_pending(channel)
    assert any("I'm Mavis" in t for t in channel.texts)
    from mavis.access.admission import admitted

    assert admitted(me) is True


async def test_full_reset_keeps_the_owner_an_active_owner(db, channel, bus, memory, invite_mode, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", f"[{CHAT}]")
    get_settings.cache_clear()
    uid = await _user(tier="owner")
    await users.update(uid, status="active")
    await _full_reset(uid, channel, bus)
    await deletion.run_deletion(bus.jobs[-1])
    me = await users.get(uid)
    assert (me.status, me.tier, me.telegram_chat_id) == ("active", "owner", CHAT)
    owners = await users.owners()
    assert [o.id for o in owners] == [uid]


async def test_a_finished_reset_job_does_not_run_twice(db, channel, bus, memory):
    uid = await _user()
    await _full_reset(uid, channel, bus)
    job = bus.jobs[-1]
    await deletion.run_deletion(job)
    await memory.graph.upsert_entity(uid, Entity(name="Fresh", label="Person"))
    await deletion.run_deletion(job)  # a retry after completion must not erase the new user's data
    assert [e.name for e in await memory.graph.entities(uid)] == ["Fresh"]


async def test_reset_does_not_say_come_back_with_an_invite(db, channel, bus, memory):
    uid = await _user()
    await _full_reset(uid, channel, bus)
    await deletion.run_deletion(bus.jobs[-1])
    await deliver_pending(channel)
    assert deletion.DONE_TEXT not in channel.texts and not any("new invite" in t for t in channel.texts)


async def test_reset_user_script_logs_counts_and_resets(db, channel, bus, memory, capsys):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "reset_user", Path(__file__).parents[2] / "scripts" / "reset_user.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    uid = await _user(tier="trusted")
    await _seed_history(uid)
    lines: list[str] = []
    await script.reset(uid, out=lines.append)
    assert any("messages: 3" in line for line in lines)
    me = await users.get(uid)
    assert (me.status, me.tier, me.telegram_chat_id) == ("active", "trusted", CHAT)
    assert await messages.recent(uid, 10) == []
    with pytest.raises(Exception, match="no user"):
        await script.reset(uid + 99, out=lines.append)
