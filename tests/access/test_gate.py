from __future__ import annotations

import pytest

from mavis.access import gate
from mavis.channels.outbox_sender import OutboxSender
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import invites, messages, users


def _ev(uid: int, text: str, n: int, at=None, etype=EventType.USER_MESSAGE) -> Event:
    return Event(id=f"g:{uid}:{n}", user_id=uid, type=etype, occurred_at=at or utcnow(), source="telegram",
                 payload={"text": text, "pending": True}, trust=Trust.USER)


@pytest.fixture
async def pending(db, invite_mode, channel):
    async def make(chat: int, name: str):
        u, _ = await users.get_or_create_by_chat(chat, name)
        return u
    return make


async def _sent(channel) -> list[str]:
    await OutboxSender(channel).run_once()
    return channel.texts


@pytest.mark.parametrize("chat,name", [(5001, "Priya"), (7302, "Tomas"), (9944, "Aiko")])
async def test_pending_user_gets_one_reply_per_day_and_no_turn(pending, channel, clock, chat, name):
    u = await pending(chat, name)
    assert await gate.access_gate(_ev(u.id, "hello", 1)) is False
    assert await gate.access_gate(_ev(u.id, "anyone?", 2)) is False
    assert await _sent(channel) == [gate.INVITE_ONLY_TEXT]
    clock.advance(hours=25)
    await gate.access_gate(_ev(u.id, "still here", 3))
    assert (await _sent(channel)).count(gate.INVITE_ONLY_TEXT) == 2
    assert await messages.recent(u.id, 10) == []


@pytest.mark.parametrize("form", ["/start MAV{c}", "MAV-{d}", "{c}"])
async def test_valid_code_activates_with_invite_defaults(pending, monkeypatch, form):
    u = await pending(6101, "Bruno")
    row, plain = await invites.mint(created_by=1, tier="trusted", tz="America/Bogota", currency="COP")
    text = form.format(c=plain, d=f"{plain[:5]}-{plain[5:]}")
    activated = []

    async def hook(usr, inv):
        activated.append((usr.id, inv.id))

    monkeypatch.setattr(gate, "on_activated", [hook])
    assert await gate.access_gate(_ev(u.id, text, 1)) is False  # the redemption message is not a turn
    fresh = await users.get(u.id)
    assert (fresh.status, fresh.tier, fresh.timezone, fresh.currency, fresh.invite_id) == (
        "active", "trusted", "America/Bogota", "COP", row.id)
    assert activated == [(u.id, row.id)]
    # a retry of the same event (now active) is still not a chat turn, a later message is
    assert await gate.access_gate(_ev(u.id, text, 1)) is False
    assert await gate.access_gate(_ev(u.id, "hello", 2)) is True


async def test_revoked_and_exhausted_codes_answer_like_unknown(pending, channel):
    a, b, c = await pending(1, "A"), await pending(2, "B"), await pending(3, "C")
    revoked, p_rev = await invites.mint(created_by=1)
    await invites.revoke(revoked.code_hint)
    _, p_one = await invites.mint(created_by=1, uses=1)
    await gate.access_gate(_ev(a.id, p_one, 1))  # uses it up
    for i, (usr, code) in enumerate([(b, p_rev), (c, p_one), (b, "ZZZZZZZZZZ")]):
        await gate.access_gate(_ev(usr.id, code, 10 + i))
    texts = await _sent(channel)
    assert texts.count(gate.CODE_NOT_VALID_TEXT) == 3
    assert (await users.get(b.id)).status == "pending" and (await users.get(c.id)).status == "pending"


async def test_brute_force_locks_the_chat_for_an_hour(pending, channel, clock, settings):
    u = await pending(8801, "Lena")
    for i in range(settings.invite_fail_limit_per_hour + 2):
        await gate.access_gate(_ev(u.id, f"AAAAA{i:05d}"[:10], i))
    assert gate.TOO_MANY_TRIES_TEXT in await _sent(channel)
    _, good = await invites.mint(created_by=1)
    await gate.access_gate(_ev(u.id, good, 99))
    assert (await users.get(u.id)).status == "pending"  # locked even for a good code
    clock.advance(hours=1, minutes=1)
    await gate.access_gate(_ev(u.id, good, 100))
    assert (await users.get(u.id)).status == "active"


async def test_banned_gets_one_reply_ever(pending, channel):
    u = await pending(4242, "Omar")
    await users.update(u.id, status="banned")
    for i in range(3):
        assert await gate.access_gate(_ev(u.id, "hi", i)) is False
    assert (await _sent(channel)).count(gate.PAUSED_TEXT) == 1


@pytest.mark.parametrize("status", ["deleting", "deleted"])
async def test_deleting_and_deleted_wakeups_are_silent(pending, channel, status):
    u = await pending(3131, "Zoe")
    await users.update(u.id, status=status)
    assert await gate.access_gate(_ev(u.id, "", 1, etype=EventType.WAKEUP)) is False
    assert await _sent(channel) == []


async def test_deleting_user_messages_are_silent(pending, channel):
    u = await pending(3133, "Mina")
    await users.update(u.id, status="deleting")
    assert await gate.access_gate(_ev(u.id, "hello", 1)) is False
    assert await _sent(channel) == []


async def test_deleted_user_returns_as_pending(pending, channel):
    u = await pending(3132, "Ines")
    await users.update(u.id, status="deleted")
    await gate.access_gate(_ev(u.id, "hi again", 1))
    assert (await users.get(u.id)).status == "pending"
    assert gate.INVITE_ONLY_TEXT in await _sent(channel)


async def test_active_users_and_system_events_pass(pending):
    u = await pending(2020, "Kai")
    await users.update(u.id, status="active")
    assert await gate.access_gate(_ev(u.id, "hi", 1)) is True
    wake = _ev(u.id, "", 2, etype=EventType.WAKEUP)
    await users.update(u.id, status="pending")
    assert await gate.access_gate(wake) is False  # nothing proactive for a non-active user


@pytest.mark.parametrize("chat,name,status", [(5001, "Priya", "pending"), (7302, "Tomas", "deleted"),
                                              (9944, "Aiko", "active")])
async def test_listed_owner_stays_in_as_admin_when_the_gate_is_on(pending, monkeypatch, channel,
                                                                  chat, name, status):
    """Grandfathering: the existing allowlisted owner is never gated, whatever the row says."""
    u = await pending(chat, name)
    await users.update(u.id, status=status)
    monkeypatch.setattr(gate.get_settings(), "owner_telegram_chat_ids", [chat])
    assert await gate.access_gate(_ev(u.id, "plan my week", 1)) is True
    fresh = await users.get(u.id)
    assert (fresh.status, fresh.tier) == ("active", "owner")
    assert await _sent(channel) == []


async def test_membership_event_leaves_groups_and_marks_blocked_private_chats(pending, channel):
    u = await pending(5150, "Rui")
    ev = Event(id="m:1", user_id=0, type=EventType.CHAT_MEMBER, occurred_at=utcnow(), source="telegram",
               trust=Trust.SYSTEM, payload={"chat_id": -4040, "chat_type": "group", "status": "member"})
    assert await gate.access_gate(ev) is False
    assert channel.left == [-4040]
    blocked = ev.model_copy(update={"id": "m:2", "payload": {"chat_id": 5150, "chat_type": "private",
                                                              "status": "kicked"}})
    await gate.access_gate(blocked)
    assert (await users.get(u.id)).inactive_since is not None


@pytest.mark.parametrize("name", ["Sam", "Rui", "Ola"])
async def test_shadow_mode_logs_but_admits(db, settings, monkeypatch, channel, name):
    from mavis.config import get_settings

    monkeypatch.setenv("ACCESS_MODE", "shadow")
    get_settings.cache_clear()
    u, _ = await users.get_or_create_by_chat(7777, name)
    assert await gate.access_gate(_ev(u.id, "hi", 1)) is True
    assert await _sent(channel) == []


async def test_allowlist_mode_admits_everything(db, settings):
    u, _ = await users.get_or_create_by_chat(7778, "Rui")
    assert await gate.access_gate(_ev(u.id, "hi", 1)) is True


async def test_default_wiring_registers_the_gate_and_defaults_admit(db, settings):
    from mavis.worker import gates
    from mavis.worker.handlers import register_default_handlers

    register_default_handlers()
    u, _ = await users.get_or_create_by_chat(7779, "Eli")
    assert await gates.run_gates(_ev(u.id, "hi", 1)) is True


async def test_crash_between_redeem_and_activate_is_recovered_by_the_retry(pending, channel, monkeypatch):
    u = await pending(6201, "Mina")
    row, plain = await invites.mint(created_by=1, uses=1)
    real = gate.activate
    calls = {"n": 0}

    async def flaky(user_id, invite, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db went away")  # after redeem committed
        return await real(user_id, invite, now)

    monkeypatch.setattr(gate, "activate", flaky)
    with pytest.raises(RuntimeError):
        await gate.access_gate(_ev(u.id, plain, 1))
    assert (await users.get(u.id)).status == "pending"
    assert await gate.access_gate(_ev(u.id, plain, 1)) is False  # the event retry
    fresh = await users.get(u.id)
    assert fresh.status == "active" and fresh.invite_id == row.id
    assert (await invites.list_active())[0].uses == 1  # one use, not two
    assert gate.CODE_NOT_VALID_TEXT not in await _sent(channel)
    assert (fresh.state or {}).get("access_gate", {}).get("fails") in (None, [])


async def test_a_retried_bad_code_event_counts_once(pending):
    u = await pending(6202, "Ola")
    ev = _ev(u.id, "MAV-AAAAA-BBBBB", 7)
    for _ in range(4):
        await gate.access_gate(ev)
    assert len((await users.get(u.id)).state["access_gate"]["fails"]) == 1
    await gate.access_gate(_ev(u.id, "MAV-AAAAA-CCCCC", 8))
    assert len((await users.get(u.id)).state["access_gate"]["fails"]) == 2


async def test_global_failed_code_counter_warns_once_at_the_limit(pending, fake_redis, monkeypatch):
    monkeypatch.setenv("INVITE_FAIL_ALERT_PER_HOUR", "3")
    from mavis.config import get_settings
    get_settings.cache_clear()
    warnings: list[str] = []
    monkeypatch.setattr(gate.log, "warning", lambda event, **kw: warnings.append(event))
    for i in range(5):  # five different chats, one bad code each: no chat reaches its own limit
        u = await pending(6300 + i, f"n{i}")
        await gate.access_gate(_ev(u.id, "MAV-AAAAA-BBBBB", 100 + i))
    assert warnings == ["gate.global_failed_codes_high"]


async def test_startup_purge_drops_stale_strangers(pending, monkeypatch):
    from datetime import timedelta

    from mavis.store.db import Session
    from mavis.store.models import User

    stale, fresh = await pending(6401, "Stale"), await pending(6402, "Fresh")
    async with Session() as s:
        (await s.get(User, stale.id)).created_at = utcnow() - timedelta(days=15)
        await s.commit()
    assert await gate.purge_strangers() == 1
    assert await users.get_by_chat(6401) is None and await users.get_by_chat(6402) is not None
    assert fresh


async def test_an_inactive_user_gets_nothing_proactive_until_they_write_again(pending):
    """inactive_since (they blocked the bot, or their chat is gone) was written and never read: briefs and
    pings were still made for people who could not receive them."""
    u = await pending(6060, "Noor")
    await users.update(u.id, status="active", inactive_since=utcnow())
    assert await gate.access_gate(_ev(u.id, "", 1, etype=EventType.WAKEUP)) is False
    assert await gate.access_gate(_ev(u.id, "", 2, etype=EventType.EMAIL_RECEIVED)) is False
    assert await gate.access_gate(_ev(u.id, "I'm back", 3)) is True
    assert (await users.get(u.id)).inactive_since is None
    assert await gate.access_gate(_ev(u.id, "", 4, etype=EventType.WAKEUP)) is True
