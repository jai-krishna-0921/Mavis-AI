from __future__ import annotations

import pytest

from mavis.store.repo import users
from mavis.tools.integrations import identity


@pytest.mark.parametrize("env", ["prod", "dev", "test"])
async def test_new_users_get_an_env_prefixed_id(db, settings, monkeypatch, env):
    from mavis.config import get_settings

    monkeypatch.setenv("ENV", env)
    get_settings.cache_clear()
    u, _ = await users.get_or_create_by_chat(5001, "Priya")
    pid = identity.new_provider_id(u.id)
    assert pid == f"mavis-{env}-{u.id}"
    await users.update(u.id, composio_user_id=pid)
    identity.clear_cache()
    assert await identity.provider_id_for(u.id) == pid
    assert await identity.user_for_provider_id(pid) == u.id


async def test_legacy_rows_keep_mavis_id(db):
    u, _ = await users.get_or_create_by_chat(7302, "Tomas")
    assert await identity.provider_id_for(u.id) == f"mavis-{u.id}"


@pytest.mark.parametrize("env,expected_ok", [("prod", True), ("dev", False), ("test", False)])
async def test_legacy_ids_are_claimed_only_in_prod(db, settings, monkeypatch, env, expected_ok):
    from mavis.config import get_settings

    u, _ = await users.get_or_create_by_chat(9944, "Aiko")
    monkeypatch.setenv("ENV", env)
    get_settings.cache_clear()
    identity.clear_cache()
    got = await identity.user_for_provider_id(f"mavis-{u.id}")
    assert (got == u.id) is expected_ok


@pytest.mark.parametrize("value", ["", None, "mavis-dev-x", "mavis-staging-3", "other-7", "mavis-12-extra"])
async def test_foreign_ids_are_not_ours(db, value):
    assert await identity.user_for_provider_id(value) is None


async def test_shared_key_check_refuses_dev_on_a_prod_project(settings, monkeypatch):
    class _P:
        async def list_user_ids(self):
            return ["mavis-1", "mavis-prod-4"]

    with pytest.raises(RuntimeError):
        await identity.check_shared_key(_P())
    monkeypatch.setenv("COMPOSIO_SHARED_KEY_OK", "true")
    from mavis.config import get_settings

    get_settings.cache_clear()
    await identity.check_shared_key(_P())


async def test_activation_stores_an_env_prefixed_composio_id(db, settings):
    from mavis.access import gate
    from mavis.store.db import utcnow
    from mavis.store.repo import invites

    u, _ = await users.get_or_create_by_chat(6001, "Lena")
    row, _ = await invites.mint(created_by=1)
    fresh = await gate.activate(u.id, row, utcnow())
    assert fresh.composio_user_id == f"mavis-{settings.env}-{u.id}"
    assert await identity.provider_id_for(u.id) == fresh.composio_user_id


@pytest.mark.parametrize("stored,external,env,resolves", [
    ("mavis-prod-5", "mavis-prod-5", "prod", True),
    ("mavis-dev-5", "mavis-prod-5", "dev", False),   # another stack's account is not ours
    (None, "mavis-5", "prod", True),                  # legacy row, prod stack
    (None, "mavis-5", "dev", False),                  # legacy row, staging stack sharing the key
])
async def test_webhook_identity_resolution(db, monkeypatch, stored, external, env, resolves):
    import base64
    import hashlib
    import hmac
    import json
    import time

    from mavis.config import get_settings
    from mavis.tools.integrations.composio import ComposioProvider

    monkeypatch.setenv("ENV", env)
    get_settings.cache_clear()
    for n in range(5):
        row, _ = await users.get_or_create_by_chat(6100 + n, f"u{n}")
    target = row.id
    assert target == 5
    if stored:
        await users.update(target, composio_user_id=stored)
    body = json.dumps({"metadata": {"trigger_slug": "GMAIL_NEW_GMAIL_MESSAGE", "user_id": external},
                       "data": {"messageId": "m9", "subject": "Hi", "sender": "a@b.com",
                                "labelIds": ["INBOX"]}}).encode()
    ts = str(int(time.time()))
    sig = base64.b64encode(hmac.new(b"sec", f"w1.{ts}.{body.decode()}".encode(), hashlib.sha256).digest())
    headers = {"webhook-id": "w1", "webhook-timestamp": ts, "webhook-signature": f"v1,{sig.decode()}"}
    events = await ComposioProvider(api_key="k", webhook_secret="sec").parse_webhook(headers, body)
    assert bool(events) is resolves
    if resolves:
        assert events[0].user_id == target


async def test_startup_refusal_stops_the_worker(settings, monkeypatch):
    from mavis.domain.errors import StartupRefused
    from mavis.worker import runner

    async def refuse() -> None:
        raise StartupRefused("no")

    async def broken() -> None:
        raise ValueError("heal failed")

    runner.clear_handlers()
    runner.register_startup_hook(broken)  # an ordinary failure is only logged
    await runner.run_startup_hooks()
    runner.register_startup_hook(refuse)
    with pytest.raises(StartupRefused):
        await runner.run_startup_hooks()
    runner.clear_handlers()
