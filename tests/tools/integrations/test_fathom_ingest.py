"""Fathom ingestion: calendar matching, meeting notes, action item loops, memory and the poll fallback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mavis import bus as bus_mod
from mavis.attention.connector_ingest import remember_identity
from mavis.domain.events import JobKind
from mavis.domain.loops import LoopKind, LoopOrigin
from mavis.memory import controls
from mavis.store.repo import loops as loops_repo
from mavis.store.repo import meeting_notes, users
from mavis.tools.integrations import fathom_ingest, fathom_poll
from mavis.tools.integrations.fathom_ingest import ingest_meeting, match_event
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from tests.tools.integrations.native.fathomfake import FakeFathom, fathom_meeting

T0 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


def iso(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def cal(eid: str, title: str, start: int, end: int, attendees: tuple[str, ...] = ()) -> dict:
    return {"id": eid, "summary": title, "start": {"dateTime": iso(start)}, "end": {"dateTime": iso(end)},
            "attendees": [{"email": a} for a in attendees]}


def meeting(rid=42, title="Budget review", start=0, end=30, invitees=(), items=(), summary="We agreed.",
            **kw) -> dict:
    return fathom_meeting(rid, title, iso(start), iso(end), invitees=invitees, action_items=items,
                          summary=summary, **kw)


@pytest.fixture
async def env(db, user, monkeypatch, recording_bus):
    fake = FakeFathom()
    api = fake.executor()[0].api
    events: list[dict] = []
    syncs: list = []
    state = SimpleNamespace(events=events, syncs=syncs, notion=False, fake=fake, uid=user.id,
                            bus=recording_bus, asked=[])

    async def calendar(uid, start, end):
        state.asked.append((start, end))
        return events

    async def notion_grant(uid):
        return Grant(uid, "notion", ACTIVE, {}) if state.notion else None

    async def enqueue_sync(uid, force=()):
        syncs.append((uid, set(force)))

    monkeypatch.setattr(fathom_ingest, "fathom_api", lambda: api)
    monkeypatch.setattr(fathom_poll, "fathom_api", lambda: api)
    monkeypatch.setattr(fathom_ingest, "calendar_events", calendar)
    monkeypatch.setattr(fathom_ingest, "notion_grant", notion_grant)
    monkeypatch.setattr(fathom_ingest, "enqueue_sync", enqueue_sync)
    bus_mod.set_bus(recording_bus)
    await remember_identity(user.id, emails=("me@acme.test",))
    yield state
    bus_mod.set_bus(None)


def test_match_prefers_overlap_then_attendees():
    m = {"title": "Budget review", "start": T0, "end": T0 + timedelta(minutes=30),
         "invitees": [{"name": "A", "email": "a@x.test"}]}
    less = cal("less", "Other", 15, 60)  # 15 minutes of overlap
    more = cal("more", "Other", 0, 30)  # 30 minutes
    assert match_event(m, [less, more])["id"] == "more"
    same_a = cal("plain", "Other", 0, 30)
    same_b = cal("shared", "Other", 0, 30, attendees=("A@x.test",))
    assert match_event(m, [same_a, same_b])["id"] == "shared"
    twin_a = cal("one", "Weekly sales", 0, 30)
    twin_b = cal("two", "Budget review", 0, 30)
    assert match_event(m, [twin_a, twin_b])["id"] == "two"
    assert match_event(m, [cal("far", "Budget review", 200, 230)]) is None
    assert match_event(m, [cal("near", "x", 40, 70)])["id"] == "near"  # starts within 15 minutes of the end
    assert match_event(m, []) is None


async def test_no_match_uses_fathom_key(env):
    key = await ingest_meeting(env.uid, meeting(rid=77), source="webhook")
    assert key == "fathom:77"
    row = await meeting_notes.get(env.uid, "fathom:77")
    assert row.recording_id == "77" and row.title == "Budget review" and row.summary == "We agreed."
    assert row.fathom_url == "https://fathom.video/share/77"
    assert row.starts_at == T0
    # the calendar was read over [start - 2h, end + 2h]
    assert env.asked == [(T0 - timedelta(hours=2), T0 + timedelta(minutes=30, hours=2))]


async def test_matched_event_gives_its_id_as_key(env):
    env.events.append(cal("evt1", "Budget review", 0, 30))
    assert await ingest_meeting(env.uid, meeting(), source="webhook") == "evt1"
    assert (await meeting_notes.get(env.uid, "evt1")).recording_id == "42"


async def test_action_items_split_commitment_waiting_skip_unassigned_and_completed(env):
    m = meeting(invitees=("Priya Nair <priya@x.test>", "me@acme.test"),
                items=("Send deck", "Review contract", "Book room", "Old thing", "Stranger task"))
    for item, (name, email) in zip(m["action_items"], [("Me", "ME@acme.test"), ("Priya Nair", "priya@x.test"),
                                                      (None, None), ("Me", "me@acme.test"),
                                                      ("Zed", "zed@elsewhere.test")], strict=True):
        item["assignee"] = {"name": name, "email": email, "team": None}
    m["action_items"][3]["completed"] = True
    await ingest_meeting(env.uid, m, source="webhook")
    got = {lp.title: lp for lp in await loops_repo.list_open(env.uid)}
    assert set(got) == {"Send deck", "Review contract"}
    assert got["Send deck"].kind is LoopKind.COMMITMENT
    assert got["Review contract"].kind is LoopKind.WAITING_ON
    assert got["Review contract"].entities == ["Priya Nair"]
    for lp in got.values():
        assert not lp.trusted and lp.origin is LoopOrigin.MEETING
        assert lp.created_ref == "fathom:42" and lp.source == "fathom:42"


async def test_ingest_is_idempotent_per_recording(env):
    m = meeting(invitees=("me@acme.test",), items=("Send deck",))
    m["action_items"][0]["assignee"] = {"name": "Me", "email": "me@acme.test", "team": None}
    env.fake.add_meeting(m, transcript=[("Priya", "Ship it.")])
    k1 = await ingest_meeting(env.uid, m, source="webhook")
    env.events.append(cal("late", "Budget review", 0, 30))  # the calendar changed in between
    first_calls = len(env.fake.requests)
    k2 = await ingest_meeting(env.uid, m, source="poll")
    assert k1 == k2 == "fathom:42"
    assert len(await loops_repo.list_open(env.uid)) == 1
    assert len(env.fake.requests) == first_calls  # no vendor call at all on the repeat
    assert len([j for j in env.bus.jobs if j.kind is JobKind.LEARN]) == 1  # and no second memory record


async def test_repeat_makes_no_recordings_calls_even_without_a_payload_summary(env):
    env.fake.add_meeting(meeting(summary="From the API."), transcript=[("Priya", "Ship it on Friday.")])
    listed = meeting(summary=None)
    await ingest_meeting(env.uid, listed, source="poll")
    seen = len(env.fake.requests)
    assert any("/recordings/" in str(r.url) for r in env.fake.requests)
    await ingest_meeting(env.uid, listed, source="webhook")
    assert not [r for r in env.fake.requests[seen:] if "/recordings/" in str(r.url)]
    assert (await meeting_notes.by_recording(env.uid, "42")).summary == "From the API."


async def test_transcript_fetch_is_limited_to_the_room_the_record_has(env):
    from mavis.memory import records

    lines = [("Priya", f"Line number {i} of the long meeting.") for i in range(400)]
    env.fake.add_meeting(meeting(), transcript=lines)
    await ingest_meeting(env.uid, meeting(summary="Short summary."), source="webhook")
    (job,) = [j for j in env.bus.jobs if j.kind is JobKind.LEARN]
    text = job.payload["text"]
    assert len(text) <= records.RECORD_TEXT_CAP
    assert "Priya: Line number 0 of the long meeting." in text and "Line number 399" not in text
    assert len(text) > records.RECORD_TEXT_CAP - 100  # the room was used, line by line


async def test_no_transcript_fetch_when_the_summary_fills_the_record(env):
    env.fake.add_meeting(meeting(), transcript=[("Priya", "Never fetched.")])
    await ingest_meeting(env.uid, meeting(summary="word " * 2000), source="webhook")
    assert not [r for r in env.fake.requests if r.url.path.endswith("/transcript")]
    (job,) = [j for j in env.bus.jobs if j.kind is JobKind.LEARN]
    assert "Never fetched." not in job.payload["text"]


async def test_memory_record_is_untrusted_with_fathom_prefix(env):
    env.fake.add_meeting(meeting(), transcript=[("Priya", "Ship it on Friday.")])
    await ingest_meeting(env.uid, meeting(invitees=("Priya Nair <priya@x.test>",)), source="webhook")
    (job,) = [j for j in env.bus.jobs if j.kind is JobKind.LEARN]
    assert job.payload["source_ref"] == "fathom:42" and job.payload["trust"] == "untrusted"
    assert job.payload["record"]["kind"] == "meeting" and job.payload["record"]["trust"] == "low"
    assert "We agreed." in job.payload["text"] and "Ship it on Friday." in job.payload["text"]
    assert "Priya Nair" in job.payload["text"]


async def test_paused_fathom_learning_skips_memory_but_keeps_notes(env):
    await controls.set_paused(env.uid, "fathom", True)
    await ingest_meeting(env.uid, meeting(), source="webhook")
    assert [j for j in env.bus.jobs if j.kind is JobKind.LEARN] == []
    assert (await meeting_notes.by_recording(env.uid, "42")) is not None


async def test_summary_fetched_when_the_payload_has_none(env):
    env.fake.add_meeting(meeting(summary="From the API."))
    listed = meeting(summary=None)
    await ingest_meeting(env.uid, listed, source="poll")
    assert (await meeting_notes.by_recording(env.uid, "42")).summary == "From the API."


async def test_notion_meetings_sync_queued_only_with_an_active_grant(env):
    await ingest_meeting(env.uid, meeting(rid=1), source="webhook")
    assert env.syncs == []
    env.notion = True
    await ingest_meeting(env.uid, meeting(rid=2), source="webhook")
    assert env.syncs == [(env.uid, {"meetings"})]


class FakePoller:
    def __init__(self, now):
        self.now = now
        self.clock = lambda: self.now

    async def set_cursor(self, uid, st, key, value):
        st.setdefault("cursors", {})[key] = value
        await users.update_nested(uid, "cursors", {key: value})


async def test_poll_fallback_every_15_min_and_skips_known(env):
    now = T0 + timedelta(hours=1)
    poller, st = FakePoller(now), {}
    known = meeting(rid=1, title="Known")
    new = meeting(rid=2, title="New", created_at=iso(50))
    env.fake.add_meeting(known)
    env.fake.add_meeting(new)
    await meeting_notes.upsert(env.uid, "k", recording_id="1", title="Known")
    assert await fathom_poll.poll_fathom(poller, env.uid, st) == 1
    (job,) = [j for j in env.bus.jobs if j.kind is JobKind.FATHOM_RECORDING]
    assert job.id == f"fathom:{env.uid}:2" and job.payload["source"] == "poll"
    assert job.payload["meeting"]["recording_id"] == 2
    cursors = st["cursors"]
    assert cursors["fathom_poll_at"] and cursors["fathom_seen"]
    calls = len(env.fake.requests)
    poller.now = now + timedelta(minutes=14)  # too soon: no vendor call
    assert await fathom_poll.poll_fathom(poller, env.uid, st) == 0
    assert len(env.fake.requests) == calls
    poller.now = now + timedelta(minutes=16)
    await fathom_poll.poll_fathom(poller, env.uid, st)
    assert len(env.fake.requests) > calls
    queued = [j for j in env.bus.jobs if j.kind is JobKind.FATHOM_RECORDING]
    assert len(queued) == 2  # re-listed and queued again: its note does not exist yet


async def test_poll_error_is_not_retried_before_15_minutes(env):
    now = T0 + timedelta(hours=1)
    poller, st = FakePoller(now), {}
    env.fake.script_error(500, n=4)
    assert await fathom_poll.poll_fathom(poller, env.uid, st) == 0
    assert st["cursors"]["fathom_poll_at"]
    calls = len(env.fake.requests)
    poller.now = now + timedelta(minutes=2)
    assert await fathom_poll.poll_fathom(poller, env.uid, st) == 0
    assert len(env.fake.requests) == calls


def _own_item(m):
    m["action_items"][0]["assignee"] = {"name": "Me", "email": "me@acme.test", "team": None}
    return m


async def test_i4_reingest_never_reopens_an_item_the_user_closed(env):
    from mavis.bus import get_bus
    from mavis.domain.loops import LoopStatus
    from mavis.loops.service import LoopService

    m = _own_item(meeting(invitees=("me@acme.test",), items=("Send deck",)))
    await ingest_meeting(env.uid, m, source="webhook")
    [loop] = await loops_repo.list_open(env.uid)
    await LoopService(get_bus()).close(loop.id, LoopStatus.DONE)
    # a repeat delivery of a known recording
    await ingest_meeting(env.uid, m, source="poll")
    assert await loops_repo.list_open(env.uid) == []
    # disconnect with forget clears the notes; the reconnect reads the same recording again
    await meeting_notes.clear_fathom(env.uid)
    await ingest_meeting(env.uid, m, source="poll")
    assert await loops_repo.list_open(env.uid) == []

