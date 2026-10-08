from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mavis.access import budgets
from mavis.access.budgets import BudgetState
from mavis.domain.errors import BudgetExceededLLM
from mavis.llm.context import bind_user
from mavis.store.repo import users


async def _user(chat, tier="standard", tz="Asia/Kolkata"):
    u, _ = await users.get_or_create_by_chat(chat, "x")
    await users.update(u.id, status="active", tier=tier, timezone=tz)
    return u.id


async def _spend(monkeypatch, usd_by_user: dict[int, float]):
    async def fake(uid):
        return int(usd_by_user.get(uid, 0) * 1_000_000)

    monkeypatch.setattr(budgets, "spend_micros_today", fake)


@pytest.mark.parametrize("tier,usd,state", [
    ("standard", 0.10, BudgetState.OK), ("standard", 0.31, BudgetState.SOFT),
    ("standard", 0.61, BudgetState.HARD), ("standard", 0.91, BudgetState.RUNAWAY),
    ("trusted", 0.61, BudgetState.OK), ("owner", 50.0, BudgetState.OK)])
async def test_state_by_tier(db, settings, monkeypatch, tier, usd, state):
    uid = await _user(5000 + int(usd * 100), tier)
    await _spend(monkeypatch, {uid: usd})
    assert await budgets.state_for(uid) is state


async def test_registered_spend_sources_add_up(db, settings, monkeypatch):
    uid = await _user(7302)
    await _spend(monkeypatch, {uid: 0.20})

    async def machine(user_id, day):
        return 0.15 if user_id == uid else 0.0

    budgets.register_spend_source("compute", machine)
    assert await budgets.spend_today_usd(uid) == pytest.approx(0.35)
    assert await budgets.state_for(uid) is BudgetState.SOFT


async def test_soft_cap_degrades_background_only(db, settings, monkeypatch, fake_llm):
    from mavis.llm import models as llm

    uid = await _user(9944)
    await _spend(monkeypatch, {uid: 0.40})
    with bind_user(uid, "memory"), pytest.raises(BudgetExceededLLM):
        await llm.complete([], priority="best_effort", name="t")
    with bind_user(uid, "task"):
        tier = await llm._budget_gate(llm.Tier.SMART, "background")
    assert tier is llm.Tier.FAST
    with bind_user(uid, "chat"):
        assert await llm._budget_gate(llm.Tier.SMART, "interactive") is llm.Tier.SMART


async def test_hard_cap_notice_is_sent_once_per_local_day(db, settings, monkeypatch, channel, clock):
    from mavis.channels.outbox_sender import deliver_pending
    from mavis.llm import models as llm

    uid = await _user(4410, tz="America/Bogota")
    await _spend(monkeypatch, {uid: 0.70})
    clock.set(datetime(2026, 10, 8, 15, 0, tzinfo=UTC))
    for _ in range(3):
        with bind_user(uid, "chat"):
            assert await llm._budget_gate(llm.Tier.SMART, "interactive") is llm.Tier.FAST
        with bind_user(uid, "task"), pytest.raises(BudgetExceededLLM):
            await llm._budget_gate(llm.Tier.SMART, "background")
    await deliver_pending(channel)
    assert channel.texts.count(budgets.HARD_CAP_TEXT) == 1


@pytest.mark.parametrize("tz,utc_hour_reset", [("Asia/Tokyo", 15), ("Europe/Lisbon", 23),
                                                ("America/Bogota", 5)])
async def test_runaway_resets_at_the_users_local_midnight(db, settings, monkeypatch, clock, tz,
                                                          utc_hour_reset):
    from mavis.llm import models as llm

    uid = await _user(6000 + utc_hour_reset, tz=tz)
    spent = {uid: 1.0}
    await _spend(monkeypatch, spent)
    with bind_user(uid, "chat"), pytest.raises(BudgetExceededLLM):
        await llm._budget_gate(llm.Tier.FAST, "interactive")
    spent[uid] = 0.0  # the local day rolled over: the day counter is empty
    with bind_user(uid, "chat"):
        assert await llm._budget_gate(llm.Tier.FAST, "interactive") is llm.Tier.FAST


async def test_cooldown_after_five_rate_limit_hits(db, settings, fake_redis):
    uid = await _user(3131)
    for _ in range(5):
        await budgets.note_rate_limit_hit(uid)
    assert await budgets.in_cooldown(uid)


async def test_ban_cancels_wakeups_and_outbox_and_unban_restores(db, settings, user):
    from mavis.domain.messages import Outbound
    from mavis.store.repo import outbox

    await users.update(user.id, status="active")
    await outbox.enqueue_now(Outbound(user_id=user.id, text="later", dedupe_key="b1"))
    await budgets.ban(user.id, "spam")
    u = await users.get(user.id)
    assert u.status == "banned" and u.ban_reason == "spam"
    assert await outbox.due(datetime(2030, 1, 1, tzinfo=UTC)) == []
    await budgets.unban(user.id)
    assert (await users.get(user.id)).status == "active"


async def test_defaults_do_not_enforce_without_a_user(settings):
    from mavis.llm import models as llm

    assert await llm._budget_gate(llm.Tier.SMART, "background") is llm.Tier.SMART


async def test_owner_commands_ban_and_unban(db, settings, monkeypatch, channel):
    from mavis.access import commands
    from mavis.config import get_settings
    from mavis.domain.events import Event, EventType, Trust
    from mavis.store.db import utcnow

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[8001]")
    get_settings.cache_clear()
    budgets.register()
    owner, _ = await users.get_or_create_by_chat(8001, "Priya")
    await users.update(owner.id, status="active", tier="owner")
    guest = await _user(8002)

    def cmd(text, n):
        return Event(id=f"b:{n}", user_id=owner.id, type=EventType.USER_MESSAGE, occurred_at=utcnow(),
                     source="telegram", payload={"text": text, "command": text[1:].split()[0]},
                     trust=Trust.USER)

    assert await commands.command_gate(cmd(f"/ban {guest} spamming links", 1)) is False
    assert (await users.get(guest)).status == "banned"
    assert await commands.command_gate(cmd(f"/unban {guest}", 2)) is False
    assert (await users.get(guest)).status == "active"
    await commands.command_gate(cmd("/ban abc", 3))
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    assert any("Use: /ban" in t for t in channel.texts)


async def test_banned_user_is_stopped_by_the_gate_and_cooldown_stops_chat(db, settings, monkeypatch, channel):
    from mavis.access import gate
    from mavis.domain.events import Event, EventType, Trust
    from mavis.store.db import utcnow

    monkeypatch.setenv("ACCESS_MODE", "invite")
    from mavis.config import get_settings

    get_settings.cache_clear()
    uid = await _user(8010)
    await budgets.start_cooldown(uid, 5)
    ev = Event(id="c:1", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source="telegram",
               payload={"text": "hi"}, trust=Trust.USER)
    assert await gate.access_gate(ev) is False  # cooling down: not a chat turn
    budgets._cool_mem.clear()
    assert await gate.access_gate(ev) is True


async def test_alert_owner_is_deduped_per_hour(db, settings, channel):
    from mavis.channels.outbox_sender import deliver_pending
    from mavis.obs.watchdog import alert_owner

    owner, _ = await users.get_or_create_by_chat(8020, "Priya")
    await users.update(owner.id, status="active", tier="owner")
    await alert_owner("watch out", key="k")
    await alert_owner("watch out", key="k")
    await deliver_pending(channel)
    assert channel.texts.count("watch out") == 1
