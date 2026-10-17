"""First sync for Notion and Fathom, and what disconnecting each one keeps or clears."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mavis import bus as bus_mod
from mavis.domain import timeutil
from mavis.domain.events import JobKind
from mavis.domain.integrations import ConnectionState
from mavis.domain.policy import Capability
from mavis.store.repo import meeting_notes, notion_links
from mavis.tools.integrations import fathom_ingest, wiring
from mavis.tools.integrations.first_sync import EXTRA_HANDLERS
from tests.tools.integrations.native.fathomfake import FakeFathom, fathom_meeting
from tests.tools.integrations.test_connect_flow_registry import make_flow

T0 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


def _iso(m):
    return (T0 + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_first_sync_notion_enqueues_and_announces(db, user, recording_bus):
    bus_mod.set_bus(recording_bus)
    try:
        lines = await wiring._notion_first_sync(user.id)
    finally:
        bus_mod.set_bus(None)
    assert lines == ["Building your Mavis AI home in Notion."]
    assert [j.kind for j in recording_bus.jobs] == [JobKind.NOTION_SYNC]


async def test_first_sync_fathom_ingests_last_14_days_capped(db, user, recording_bus, monkeypatch):
    fake = FakeFathom()
    api = fake.executor()[0].api
    now = timeutil.now()
    for rid in range(1, 26):  # 25 recent meetings, plus one older than 14 days
        start = now - timedelta(days=1, minutes=rid)
        fake.add_meeting(fathom_meeting(rid, f"M{rid}", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        (start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    old = now - timedelta(days=30)
    fake.add_meeting(fathom_meeting(99, "Old", old.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    (old + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    monkeypatch.setattr(fathom_ingest, "fathom_api", lambda: api)
    bus_mod.set_bus(recording_bus)
    try:
        lines = await wiring._fathom_first_sync(user.id)
    finally:
        bus_mod.set_bus(None)
    jobs = [j for j in recording_bus.jobs if j.kind is JobKind.FATHOM_RECORDING]
    assert len(jobs) == 20 and all(j.payload["source"] == "poll" for j in jobs)
    assert "99" not in {j.id.rsplit(":", 1)[1] for j in jobs}
    assert lines == ["Reading 20 recent Fathom meetings for notes and action items."]


async def test_builtin_composio_notion_first_sync_no_longer_wins(db, user, recording_bus):
    fs = wiring.get_first_sync()
    assert EXTRA_HANDLERS[Capability.NOTION] is wiring._notion_first_sync
    assert EXTRA_HANDLERS[Capability.FATHOM] is wiring._fathom_first_sync
    bus_mod.set_bus(recording_bus)
    try:
        lines = await fs.run(user.id, Capability.NOTION)
    finally:
        bus_mod.set_bus(None)
    assert lines == ["Building your Mavis AI home in Notion."]


async def test_disconnect_forget_fathom_clears_fathom_notes_keeps_user_notes(
    db, user, provider, cache, fake_bus, rec, state
):
    uid = user.id
    await meeting_notes.upsert(uid, "fathom:1", title="Only Fathom", recording_id="1",
                               fathom_url="https://f/1", summary="s", action_items=[{"description": "x"}])
    await meeting_notes.upsert(uid, "evt-2", title="Mine", recording_id="2", fathom_url="https://f/2",
                               summary="s2", user_notes="my own words")
    provider.set_state(uid, Capability.FATHOM, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.disconnect(uid, Capability.FATHOM, forget=True) is True
    assert await meeting_notes.get(uid, "fathom:1") is None
    kept = await meeting_notes.get(uid, "evt-2")
    assert kept.user_notes == "my own words" and not kept.summary and not kept.recording_id


async def test_disconnect_without_forget_keeps_fathom_notes(db, user, provider, cache, fake_bus, rec, state):
    await meeting_notes.upsert(user.id, "fathom:1", title="T", recording_id="1", summary="s")
    provider.set_state(user.id, Capability.FATHOM, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    await flow.disconnect(user.id, Capability.FATHOM, forget=False)
    assert (await meeting_notes.get(user.id, "fathom:1")).summary == "s"


async def test_disconnect_notion_keeps_links_and_pages(db, user, provider, cache, fake_bus, rec, state):
    await notion_links.upsert(user.id, "todo", "7", "page-7", pushed_hash="h")
    await notion_links.upsert(user.id, "home", "home", "page-home")
    provider.set_state(user.id, Capability.NOTION, ConnectionState.ACTIVE)
    flow = make_flow(provider, cache, fake_bus, rec, state)
    assert await flow.disconnect(user.id, Capability.NOTION, forget=True) is True
    assert (await notion_links.get(user.id, "todo", "7")).page_id == "page-7"
    assert (await notion_links.get(user.id, "home", "home")).page_id == "page-home"


async def test_disconnect_clears_notion_and_fathom_cursors(db, user, provider, cache, fake_bus, rec, state):
    await state.update(user.id, {"cursors": {"notion_setup": "done", "fathom_seen": "x", "gmail_x": "k"}})
    flow = make_flow(provider, cache, fake_bus, rec, state)
    for cap in (Capability.NOTION, Capability.FATHOM):
        provider.set_state(user.id, cap, ConnectionState.ACTIVE)
        await flow.disconnect(user.id, cap, forget=False)
    cursors = (await state.get(user.id))["cursors"]
    assert "notion_setup" not in cursors and "fathom_seen" not in cursors
    assert cursors.get("gmail_x") == "k"


async def test_i4_first_sync_skips_recordings_that_already_have_a_note(db, user, recording_bus, monkeypatch):
    from mavis.store.repo import meeting_notes

    fake = FakeFathom()
    api = fake.executor()[0].api
    now = timeutil.now()
    for rid in (1, 2):
        start = now - timedelta(days=1, minutes=rid)
        fake.add_meeting(fathom_meeting(rid, f"M{rid}", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        (start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    await meeting_notes.upsert(user.id, "fathom:1", recording_id="1", title="M1")
    monkeypatch.setattr(fathom_ingest, "fathom_api", lambda: api)
    bus_mod.set_bus(recording_bus)
    try:
        lines = await wiring._fathom_first_sync(user.id)
    finally:
        bus_mod.set_bus(None)
    jobs = [j for j in recording_bus.jobs if j.kind is JobKind.FATHOM_RECORDING]
    assert [j.id.rsplit(":", 1)[1] for j in jobs] == ["2"]
    assert lines == ["Reading 1 recent Fathom meeting for notes and action items."]
