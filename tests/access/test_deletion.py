from __future__ import annotations

import pytest

from mavis.access import deletion
from mavis.domain.messages import Role
from mavis.store import models  # noqa: F401
from mavis.store.db import Base
from mavis.store.repo import deletion as repo
from mavis.store.repo import messages, users


def test_every_user_table_is_in_the_cascade():
    """Any ORM table with a user_id column must be deleted with the user (contract F: tables added by other
    plans, such as commitments or the sandbox tables, must be listed by whichever plan merges second)."""
    with_user = {t.name for t in Base.metadata.sorted_tables if "user_id" in t.c}
    assert with_user - set(repo.USER_TABLES) == set()


def test_cascade_order_is_fk_safe():
    order = {name: i for i, name in enumerate(repo.USER_TABLES)}
    for t in Base.metadata.sorted_tables:
        for fk in t.foreign_keys:
            if t.name in order and fk.column.table.name in order and fk.column.table is not t:
                assert order[t.name] < order[fk.column.table.name], (t.name, fk.column.table.name)


@pytest.fixture(autouse=True)
def _installed_provider(provider, monkeypatch, memory_checkpointer):
    import functools

    monkeypatch.setattr("mavis.tools.integrations.get_provider", functools.cache(lambda: provider))


async def _seed(chat: int, name: str) -> int:
    u, _ = await users.get_or_create_by_chat(chat, name)
    await users.update(u.id, status="active", composio_user_id=f"mavis-test-{u.id}")
    for i in range(3):
        await messages.log(u.id, Role.USER, f"{name} message {i}", event_id=f"d:{u.id}:{i}")
    return u.id


@pytest.mark.parametrize("names", [("Priya", "Tomas"), ("Aiko", "Bruno")])
async def test_deletion_removes_only_that_user(db, memory, provider, names):
    a, b = await _seed(5001, names[0]), await _seed(7302, names[1])
    await deletion.run_steps(a)
    assert await messages.recent(a, 10) == [] and len(await messages.recent(b, 10)) == 3
    assert (await users.get(a)).status == "deleted"


async def test_deletion_resumes_after_a_crash_mid_way(db, memory, provider, monkeypatch):
    uid = await _seed(9944, "Lena")
    calls = []

    async def flaky(user_id):
        calls.append(user_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": 1}

    repo.register_deletion_step("flaky", flaky)
    with pytest.raises(RuntimeError):
        await deletion.run_steps(uid)
    state = (await users.get_state(uid)).get("deletion", {})
    assert "redis" in state.get("done", []) and "flaky" not in state.get("done", [])
    await deletion.run_steps(uid)
    assert calls == [uid, uid] and (await users.get(uid)).status == "deleted"


async def test_tombstone_has_no_personal_fields(db, memory, provider):
    uid = await _seed(4410, "Omar")
    await deletion.run_steps(uid)
    u = await users.get(uid)
    assert (u.name, u.telegram_chat_id, u.telegram_user_id, u.composio_user_id, u.state) == (
        None, None, None, None, {})
    assert u.deleted_at is not None


async def test_confirm_flow_buttons_and_expiry(db, channel, clock, invite_mode):
    uid = await _seed(3131, "Zoe")
    await deletion.start(uid, event_id="e1")
    clock.advance(minutes=11)
    assert await deletion.confirm(uid, data=f"del:yes:{uid}") is False  # expired, nothing happens
    await deletion.start(uid, event_id="e2")
    assert await deletion.confirm(uid, data=f"del:yes:{uid}") is True
    assert (await users.get(uid)).status == "deleting"


async def test_delete_me_command_asks_then_the_button_starts_the_job(db, channel, bus, settings, invite_mode):
    from mavis.access import commands
    from mavis.channels.outbox_sender import deliver_pending
    from mavis.domain.events import Event, EventType, Trust
    from mavis.store.db import utcnow

    deletion.register()
    uid = await _seed(6001, "Rui")
    ev = Event(id="dm:1", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source="telegram",
               payload={"text": "/delete_me", "command": "delete_me"}, trust=Trust.USER)
    assert await commands.command_gate(ev) is False
    await deliver_pending(channel)
    assert channel.texts == [deletion.CONFIRM_TEXT]
    labels = [b.label for row in channel.sent[-1].buttons for b in row]
    assert labels == ["Delete everything", "Cancel"]
    assert await deletion.confirm(uid, f"del:no:{uid}") is False
    assert (await users.get(uid)).status == "active"  # cancelling changes nothing
    await deletion.start(uid, "dm:2")
    assert await deletion.confirm(uid, f"del:yes:{uid + 1}") is False  # a button meant for someone else
    assert await deletion.confirm(uid, f"del:yes:{uid}") is True
    assert (await users.get(uid)).status == "deleting"


async def test_events_of_a_deleting_user_are_dropped_by_the_gate(db, invite_mode):
    from mavis.access import gate
    from mavis.domain.events import Event, EventType, Trust
    from mavis.store.db import utcnow

    uid = await _seed(6002, "Ola")
    await users.update(uid, status="deleting")
    ev = Event(id="x:1", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source="telegram",
               payload={"text": "hello"}, trust=Trust.USER)
    assert await gate.access_gate(ev) is False


async def test_a_deleted_chat_that_returns_is_a_stranger_again(db, memory, invite_mode, recording_bus):
    from mavis.channels.telegram_updates import ingest_update

    uid = await _seed(6003, "Mina")
    await deletion.run_steps(uid)
    update = {"update_id": 9, "message": {"message_id": 9, "date": 1760000000, "text": "hi",
                                          "chat": {"id": 6003, "type": "private"},
                                          "from": {"id": 6003, "first_name": "Mina"}}}

    async def noop(_cid):
        return None

    assert await ingest_update(update, recording_bus, noop)
    fresh = await users.get_by_chat(6003)
    assert fresh.id != uid and fresh.status == "pending"


async def test_every_store_is_emptied(db, memory, provider, fake_redis, settings, fake_llm):
    from mavis.domain.memory import Extraction
    from mavis.store import artifacts
    from mavis.store.repo import tasks

    uid = await _seed(6004, "Kai")
    fake_llm.push_structured(Extraction())
    await memory.learn(uid, "My sister lives in Porto", source_ref="del:1")
    provider.states[uid] = {"gmail": "ACTIVE", "slack": "ACTIVE"}
    d = artifacts.user_dir(uid, 5)
    d.mkdir(parents=True)
    (d / "deck.pptx").write_bytes(b"PK")
    await fake_redis.set(f"mavis:spend:u{uid}:20261008", 5)
    await fake_redis.set(f"mavis:spend:u{uid + 1}:20261008", 7)  # another user's key survives
    await tasks.create(uid, goal="secret goal")
    assert await memory.vector.count(uid) > 0
    report = await deletion.run_steps(uid)
    assert report["composio"] == {"accounts": 2} and provider.states.get(uid) is None
    assert await memory.vector.count(uid) == 0 and await memory.graph.dump(uid) == []
    assert not artifacts.user_dir(uid).exists()
    assert await fake_redis.get(f"mavis:spend:u{uid}:20261008") is None
    assert await fake_redis.get(f"mavis:spend:u{uid + 1}:20261008") == "7"
    assert report["postgres"]["tasks"] == 1 and report["checkpoints"] == {"threads": 1}


async def test_owner_admin_delete_command(db, settings, bus, monkeypatch):
    from mavis.access import commands
    from mavis.config import get_settings
    from mavis.domain.events import Event, EventType, Trust
    from mavis.store.db import utcnow

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[6100]")
    get_settings.cache_clear()
    deletion.register()
    owner, _ = await users.get_or_create_by_chat(6100, "Priya")
    await users.update(owner.id, status="active", tier="owner")
    victim = await _seed(6101, "Troll")
    ev = Event(id="ad:1", user_id=owner.id, type=EventType.USER_MESSAGE, occurred_at=utcnow(),
               source="telegram", payload={"text": f"/admin_delete delete {victim}",
                                           "command": "admin_delete"}, trust=Trust.USER)
    assert await commands.command_gate(ev) is False
    assert (await users.get(victim)).status == "deleting"
