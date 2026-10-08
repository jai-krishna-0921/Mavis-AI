from __future__ import annotations

import pytest

from mavis.access import gate, preferences
from mavis.agents import onboarding
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import invites, users


async def _activate(chat: int, name: str, tz: str | None, cur: str | None):
    u, _ = await users.get_or_create_by_chat(chat, name)
    row, _ = await invites.mint(created_by=1, tz=tz, currency=cur)
    onboarding.register()
    return await gate.activate(u.id, row, utcnow()), row


async def _texts(channel) -> list[str]:
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    return channel.texts


@pytest.mark.parametrize("name,tz,cur,symbol", [("Priya", "Asia/Kolkata", None, "INR"),
                                                ("Tomas", "Europe/Lisbon", None, "EUR"),
                                                ("Aiko", "Asia/Tokyo", "JPY", "JPY")])
async def test_welcome_clock_check_and_currency_line(db, channel, name, tz, cur, symbol):
    u, _ = await _activate(5000 + len(name), name, tz, cur)
    out = await _texts(channel)
    assert out[0].startswith(f"Hi {name}, I'm Mavis")
    assert "Quick check: is it" in out[1] and "where you are?" in out[1]
    labels = [b.label for row in channel.sent[1].buttons for b in row]
    assert labels == ["Yes", "No"]
    assert all("\u2014" not in t and "\u2013" not in t for t in out)
    assert (await users.get(u.id)).currency == symbol


async def test_no_then_city_with_ambiguity_offers_buttons(db, channel, settings, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(onboarding, "CITY_TABLE", Path(__file__).parent / "fixtures" / "cities_small.tsv")
    u, _ = await _activate(6201, "Lena", "Asia/Kolkata", None)
    await onboarding.on_button(_btn(u.id, "ob:tz:no"), "ob:tz:no")
    await onboarding.on_text(u, "Portland")
    out = await _texts(channel)
    labels = [b.label for row in channel.sent[-1].buttons for b in row]
    assert len(labels) == 2 and all("Portland" in lbl for lbl in labels)
    await onboarding.on_button(_btn(u.id, "ob:tz:1"), "ob:tz:1")
    assert (await users.get(u.id)).timezone == "America/New_York"
    assert any("What's one thing you want off your mind this week?" in t for t in await _texts(channel))
    assert "\u2014" not in "".join(out)


async def test_location_answer_sets_zone_and_currency(db, channel):
    u, _ = await _activate(6202, "Bruno", None, None)
    await onboarding.on_button(_btn(u.id, "ob:tz:no"), "ob:tz:no")
    await onboarding.on_location(u, 4.71, -74.07)
    fresh = await users.get(u.id)
    assert fresh.timezone == "America/Bogota" and fresh.currency == "COP"


async def test_set_timezone_runs_hooks_and_keeps_one_off_wakeups(db, user):
    seen = []

    async def hook(uid, old, new):
        seen.append((uid, old, new))

    preferences.register_timezone_hook(hook)
    change = await preferences.set_timezone(user.id, "America/Bogota")
    assert change.new == "America/Bogota" and seen == [(user.id, change.old, "America/Bogota")]
    with pytest.raises(ValueError):
        await preferences.set_timezone(user.id, "Nowhere/Land")


def _btn(uid: int, data: str) -> Event:
    return Event(id=f"b:{uid}:{data}", user_id=uid, type=EventType.BUTTON_PRESSED, occurred_at=utcnow(),
                 source="telegram", payload={"data": data}, trust=Trust.USER)


async def test_connector_offer_follows_the_first_turn(db, channel):
    from datetime import timedelta

    from mavis.domain.wakeups import WakeupKind
    from mavis.timers.service import WakeupService

    u, _ = await _activate(6301, "Mina", "Europe/Lisbon", None)
    await onboarding.on_button(_btn(u.id, "ob:tz:yes"), "ob:tz:yes")
    chat = Event(id="m:1", user_id=u.id, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source="telegram",
                 payload={"text": "I need to call the dentist"}, trust=Trust.USER)
    assert await onboarding.onboarding_gate(chat) is True  # a normal turn
    (wake,) = await WakeupService().pending(u.id, WakeupKind.SYSTEM_ONBOARD_CONNECT)
    assert wake.due_at - utcnow() < timedelta(seconds=61)
    await onboarding.after_first_value(u.id)
    await onboarding.after_first_value(u.id)  # a repeat is a no-op
    texts = await _texts(channel)
    assert texts.count(onboarding.CONNECT) == 1
    labels = [b.label for row in channel.sent[-1].buttons for b in row]
    assert labels == ["Connect Google", "Later"]


@pytest.mark.parametrize("text", ["hello", "/settings"])
async def test_established_users_are_not_captured_by_the_onboarding_gate(db, channel, user, text):
    ev = Event(id="m:9", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=utcnow(),
               source="telegram", payload={"text": text, "location": {"latitude": 4.7, "longitude": -74.0}},
               trust=Trust.USER)
    assert await onboarding.onboarding_gate(ev) is True
    assert (await users.get(user.id)).timezone == user.timezone


async def test_settings_command_shows_values_and_offers_the_clock_check(db, channel):
    from mavis.access import commands

    u, _ = await _activate(6302, "Ines", "Europe/Lisbon", "EUR")
    ev = Event(id="s:1", user_id=u.id, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source="telegram",
               payload={"text": "/settings", "command": "settings"}, trust=Trust.USER)
    assert await commands.command_gate(ev) is False
    out = await _texts(channel)
    assert any("Time zone: Europe/Lisbon" in t and "Currency: EUR" in t for t in out)
    assert any("Quick check" in t for t in out)
    assert all("—" not in t for t in out)


@pytest.mark.parametrize("args,needle,field,value", [
    ({"timezone": "Asia/Tokyo"}, "time zone Asia/Tokyo", "timezone", "Asia/Tokyo"),
    ({"currency": "jpy"}, "currency JPY", "currency", "JPY"),
    ({"name": "  Kai   Tanaka "}, "name Kai Tanaka", "name", "Kai Tanaka"),
    ({"locale": "pt-PT"}, "locale pt-PT", "locale", "pt-PT"),
])
async def test_set_preferences_tool_updates_the_user(db, user, args, needle, field, value):
    from mavis.tools.preferences import TOOLS, SetPreferencesArgs

    out = await TOOLS[0].fn(user.id, SetPreferencesArgs(**args))
    assert needle in out.user_text
    assert getattr(await users.get(user.id), field) == value


async def test_set_preferences_tool_rejects_a_bad_zone_without_changing_anything(db, user):
    from mavis.tools.preferences import TOOLS, SetPreferencesArgs

    out = await TOOLS[0].fn(user.id, SetPreferencesArgs(timezone="Nowhere/Land"))
    assert "Unknown time zone" in out.model_note
    assert (await users.get(user.id)).timezone == user.timezone


async def test_timezone_change_moves_the_morning_checkin_to_the_new_zone(db, user, fake_redis):
    from mavis.domain import timeutil
    from mavis.domain.wakeups import WakeupKind
    from mavis.initiative.wiring import wire_initiative
    from mavis.timers.service import WakeupService

    init = wire_initiative(register_handlers=False)
    await init.routines.on_user_message(await users.get(user.id))
    (before,) = await WakeupService().pending(user.id, WakeupKind.ROUTINE)
    await preferences.set_timezone(user.id, "Pacific/Auckland")
    (after,) = await WakeupService().pending(user.id, WakeupKind.ROUTINE)
    assert after.id != before.id
    hour = timeutil.to_local(after.due_at, "Pacific/Auckland").hour
    assert hour == timeutil.to_local(before.due_at, user.timezone).hour
