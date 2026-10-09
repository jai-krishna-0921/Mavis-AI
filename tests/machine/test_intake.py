from __future__ import annotations

import pytest

from mavis import machine
from mavis.channels import set_channel
from mavis.channels.fake import FakeChannel
from mavis.domain.events import Event, EventType, Trust
from mavis.machine import intake
from mavis.machine.fake import FakeSandbox, MemoryWorkspaceStore
from mavis.machine.ports import Provenance
from mavis.machine.runtime import MachineRuntime
from mavis.store.db import utcnow


@pytest.fixture
def on(settings, monkeypatch):
    monkeypatch.setattr(settings, "machine_enabled", True)
    rt = MachineRuntime(FakeSandbox(), MemoryWorkspaceStore())
    machine.set_runtime(rt)
    ch = FakeChannel()
    set_channel(ch)
    yield rt, ch
    set_channel(None)


def _ev(user_id, name, size=1200, eid="tg:update:1"):
    return Event(
        id=eid,
        user_id=user_id,
        type=EventType.USER_MESSAGE,
        occurred_at=utcnow(),
        source="telegram",
        trust=Trust.USER,
        payload={"text": "analyse this", "file": {"file_id": "AgAD-x", "file_name": name, "size": size}},
    )


@pytest.mark.parametrize(
    "name,stored",
    [
        ("Q3 sales.csv", "inbox/Q3_sales.csv"),
        ("../../x.pdf", "inbox/x.pdf"),
        ("notes.txt", "inbox/notes.txt"),
    ],
)
async def test_document_lands_in_inbox_as_an_untrusted_upload(db, user, on, name, stored):
    rt, _ = on
    await intake.on_user_message(_ev(user.id, name))
    meta = await rt.store.meta(user.id, stored)
    assert meta is not None and meta.provenance is Provenance.USER_UPLOAD


async def test_same_event_twice_is_one_file(db, user, on):
    rt, _ = on
    await intake.on_user_message(_ev(user.id, "a.csv"))
    await intake.on_user_message(_ev(user.id, "a.csv"))
    assert len(await rt.store.list(user.id)) == 1


@pytest.mark.parametrize("mb", [21, 50, 400])
async def test_too_big_is_explained_not_downloaded(db, user, on, sent, mb):
    rt, _ = on
    await intake.on_user_message(_ev(user.id, "big.zip", size=mb * 1024 * 1024))
    assert await rt.store.list(user.id) == []
    assert any("20 MB" in m.text for m in sent) and not any("\u2014" in m.text for m in sent)


async def test_off_or_not_allowed_does_nothing(db, user, settings, monkeypatch):
    monkeypatch.setattr(settings, "machine_enabled", True)
    monkeypatch.setattr(settings, "machine_users", [user.id + 1000])
    rt = MachineRuntime(FakeSandbox(), MemoryWorkspaceStore())
    machine.set_runtime(rt)
    await intake.on_user_message(_ev(user.id, "a.csv"))
    assert await rt.store.list(user.id) == []


async def test_inbox_context_lists_recent_names_only(db, user, on):
    rt, _ = on
    for n in ("one.csv", "two.pdf"):
        await rt.store.put(user.id, f"inbox/{n}", b"x" * 2048, provenance=Provenance.USER_UPLOAD, cls=None)
    block = await intake.inbox_context(user.id, "what did I send?")
    assert "one.csv" in block and "two.pdf" in block and "x" * 10 not in block
