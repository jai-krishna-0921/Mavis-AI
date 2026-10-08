from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from mavis.access.codes import InviteError
from mavis.store.db import utcnow
from mavis.store.repo import invites, users


async def _user(chat: int, name: str) -> int:
    u, _ = await users.get_or_create_by_chat(chat, name)
    return u.id


@pytest.mark.parametrize("tier,tz,cur", [("standard", "Europe/Lisbon", "EUR"),
                                         ("trusted", "Asia/Tokyo", "JPY"), ("standard", None, None)])
async def test_mint_stores_only_the_hash_and_redeems_once(db, tier, tz, cur):
    row, plain = await invites.mint(created_by=1, uses=1, days=3, tier=tier, tz=tz, currency=cur, label="x")
    assert plain not in (row.code_hash, row.code_hint) and row.code_hint == plain[-4:]
    a, b = await _user(5001, "Priya"), await _user(7302, "Tomas")
    got = await invites.redeem(plain, a, utcnow())
    assert got is not None and got.tier == tier and got.default_timezone == tz and got.uses == 1
    assert await invites.redeem(plain, b, utcnow()) is None  # exhausted
    assert [r.user_id for r in await invites.redemptions_for(got.id)] == [a]


async def test_revoked_expired_and_unknown_all_return_none(db):
    uid = await _user(9944, "Aiko")
    revoked, p1 = await invites.mint(created_by=1)
    await invites.revoke(revoked.code_hint)
    _, p2 = await invites.mint(created_by=1, days=1)
    assert await invites.redeem(p1, uid, utcnow()) is None
    assert await invites.redeem(p2, uid, utcnow() + timedelta(days=2)) is None
    assert await invites.redeem("ZZZZZZZZZZ", uid, utcnow()) is None


async def test_limits_on_active_codes_and_uses(db, settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("INVITE_MAX_ACTIVE", "3")
    monkeypatch.setenv("INVITE_MAX_USES", "4")
    get_settings.cache_clear()
    with pytest.raises(InviteError) as exc:
        await invites.mint(created_by=1, uses=5)
    assert exc.value.reason == "limit"
    for _ in range(3):
        await invites.mint(created_by=1)
    with pytest.raises(InviteError):
        await invites.mint(created_by=1)
    assert len(await invites.list_active()) == 3


async def test_concurrent_redemptions_never_exceed_max_uses(db):
    _, plain = await invites.mint(created_by=1, uses=2)
    ids = [await _user(6000 + i, f"u{i}") for i in range(6)]
    results = await asyncio.gather(*(invites.redeem(plain, uid, utcnow()) for uid in ids))
    assert sum(r is not None for r in results) == 2


async def test_revoke_matches_an_all_digit_hint_before_an_id(db):
    from mavis.store.db import Session
    from mavis.store.models import InviteCode

    row, _ = await invites.mint(created_by=1)
    async with Session() as s:
        r = await s.get(InviteCode, row.id)
        r.code_hint = "0042"
        await s.commit()
    got = await invites.revoke("0042")
    assert got is not None and got.id == row.id
