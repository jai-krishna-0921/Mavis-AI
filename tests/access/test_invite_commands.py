from __future__ import annotations

import pytest

from mavis.access import commands, invite_commands
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import invites, users


@pytest.fixture
async def owner(db, settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[5001]")
    get_settings.cache_clear()
    u, _ = await users.get_or_create_by_chat(5001, "Priya")
    await users.update(u.id, status="active", tier="owner")
    invite_commands.register()
    return await users.get(u.id)


def _cmd(uid: int, text: str, n: int = 1) -> Event:
    return Event(id=f"c:{uid}:{n}", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(),
                 source="telegram", payload={"text": text, "command": text[1:].split()[0]}, trust=Trust.USER)


async def _replies(channel) -> list[str]:
    from mavis.channels.outbox_sender import deliver_pending

    await deliver_pending(channel)
    return channel.texts


async def test_invite_new_with_options_replies_with_code_and_link(owner, channel):
    assert await commands.command_gate(_cmd(owner.id, "/invite new uses=3 days=7 tier=trusted "
                                                      "tz=Europe/Lisbon cur=eur family friends")) is False
    (reply,) = await _replies(channel)
    assert "MAV-" in reply and "https://telegram.me/" in reply and "?start=MAV" in reply
    (row,) = await invites.list_active()
    assert (row.max_uses, row.tier, row.default_timezone, row.default_currency, row.label) == (
        3, "trusted", "Europe/Lisbon", "EUR", "family friends")


@pytest.mark.parametrize("bad", ["tz=Mars/Base", "cur=EURO", "uses=0", "tier=god"])
async def test_invalid_options_are_refused_without_minting(owner, channel, bad):
    await commands.command_gate(_cmd(owner.id, f"/invite new {bad}"))
    assert await invites.list_active() == []
    assert "\u2014" not in (await _replies(channel))[0]


async def test_list_revoke_and_users(owner, channel):
    row, plain = await invites.mint(created_by=owner.id, uses=2, label="book club")
    u2, _ = await users.get_or_create_by_chat(7302, "Tomas")
    await invites.redeem(plain, u2.id, utcnow())
    await commands.command_gate(_cmd(owner.id, "/invite list", 2))
    await commands.command_gate(_cmd(owner.id, f"/invite users {row.id}", 3))
    await commands.command_gate(_cmd(owner.id, f"/invite revoke {row.code_hint}", 4))
    out = await _replies(channel)
    assert "book club" in out[0] and "1/2" in out[0]
    assert "Tomas" in out[1]
    assert await invites.list_active() == []


@pytest.mark.parametrize("chat,tier", [(7302, "standard"), (9944, "trusted"), (5002, "owner")])
async def test_non_owner_commands_fall_through_to_normal_handling(owner, channel, chat, tier):
    u, _ = await users.get_or_create_by_chat(chat, "X")
    await users.update(u.id, status="active", tier=tier)  # tier owner but chat not in the owner list
    assert await commands.command_gate(_cmd(u.id, "/invite new")) is True
    assert await invites.list_active() == []


@pytest.mark.parametrize("text,needle", [
    ("/invite bogus", "/invite new"),
    ("/invite revoke ZZZZ", "No open code"),
    ("/invite users 999", "Nobody"),
    ("/invite new uses=lots", "whole numbers"),
    ("/invite list", "No open invite"),
])
async def test_owner_subcommand_edges(owner, channel, text, needle):
    assert await commands.command_gate(_cmd(owner.id, text)) is False
    (reply,) = await _replies(channel)
    assert needle in reply and "—" not in reply and "–" not in reply


async def test_plain_text_and_unknown_commands_pass_through(owner, channel):
    assert await commands.command_gate(_cmd(owner.id, "/connect gmail")) is True
    assert await commands.command_gate(_cmd(owner.id, "hello there")) is True
    assert await _replies(channel) == []


async def test_new_invite_deep_link_uses_the_configured_bot_name(owner, channel, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "MavisTestBot")
    get_settings.cache_clear()
    await commands.command_gate(_cmd(owner.id, "/invite new"))
    (reply,) = await _replies(channel)
    assert "https://telegram.me/MavisTestBot?start=MAV" in reply
