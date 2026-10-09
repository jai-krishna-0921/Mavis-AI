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


async def test_redeem_is_idempotent_per_user_so_a_crash_retry_does_not_burn_a_use(db):
    row, plain = await invites.mint(created_by=1, uses=1)
    a, b = await _user(5101, "Priya"), await _user(5102, "Tomas")
    first = await invites.redeem(plain, a, utcnow())
    again = await invites.redeem(plain, a, utcnow())  # the retry after a crash before activation
    assert first is not None and again is not None and again.id == first.id and again.uses == 1
    assert len(await invites.redemptions_for(row.id)) == 1
    assert await invites.redeem(plain, b, utcnow()) is None  # still exhausted for anyone else


@pytest.mark.parametrize("tier", ["owner", "god", "", "STANDARD"])
async def test_mint_rejects_unknown_and_owner_tiers(db, tier):
    with pytest.raises(InviteError):
        await invites.mint(created_by=1, tier=tier)
    assert await invites.list_active() == []


async def test_purge_strangers_removes_only_old_pending_rows_without_redemptions(db):
    from datetime import timedelta

    from mavis.store.db import Session
    from mavis.store.models import User
    from mavis.store.repo.users import purge_strangers

    _, plain = await invites.mint(created_by=1)
    old, redeemed, fresh, owner = [await _user(c, n) for c, n in
                                   [(5201, "Old"), (5202, "Redeemed"), (5203, "Fresh"), (5204, "Owner")]]
    await invites.redeem(plain, redeemed, utcnow())
    async with Session() as s:
        for uid in (old, redeemed, owner):
            (await s.get(User, uid)).created_at = utcnow() - timedelta(days=30)
        await s.commit()
    assert await purge_strangers(utcnow() - timedelta(days=14), frozenset({5204})) == 1
    left = [await users.get_by_chat(c) is not None for c in (5201, 5202, 5203, 5204)]
    assert left == [False, True, True, True]
    assert fresh


async def test_concurrent_redemptions_on_postgres_never_exceed_max_uses(monkeypatch):
    """Needs a Postgres server: set TEST_POSTGRES_URL (postgresql+psycopg://user:pw@host:port/postgres). It
    creates and drops its own throwaway database."""
    import os
    import uuid

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from mavis.config import get_settings
    from mavis.store.db import dispose_engine, init_db

    admin_url = os.environ.get("TEST_POSTGRES_URL")
    if not admin_url:
        pytest.skip("TEST_POSTGRES_URL not set")
    name = f"mavis_t_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception:  # noqa: BLE001
        await admin.dispose()
        pytest.skip("postgres not reachable")
    try:
        monkeypatch.setenv("DATABASE_URL", admin_url.rsplit("/", 1)[0] + "/" + name)
        get_settings.cache_clear()
        await dispose_engine()
        await init_db()
        _, plain = await invites.mint(created_by=None, uses=3)
        ids = [await _user(7000 + i, f"u{i}") for i in range(12)]
        results = await asyncio.gather(*(invites.redeem(plain, uid, utcnow()) for uid in ids))
        assert sum(r is not None for r in results) == 3
        assert (await invites.list_active())[0].uses == 3
    finally:
        await dispose_engine()
        get_settings.cache_clear()
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
