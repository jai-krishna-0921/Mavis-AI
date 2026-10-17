from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from mavis.domain import timeutil
from mavis.store.repo import meeting_notes


async def test_meeting_notes_unique_recording(user):
    await meeting_notes.upsert(user.id, "ev1", title="A", recording_id="r1")
    with pytest.raises(IntegrityError):
        await meeting_notes.upsert(user.id, "ev2", title="B", recording_id="r1")
    assert (await meeting_notes.by_recording(user.id, "r1")).event_key == "ev1"
    row = await meeting_notes.upsert(user.id, "ev1", summary="s")
    assert row.title == "A" and row.summary == "s"


async def test_append_user_notes_joins_with_blank_line(user):
    first = await meeting_notes.append_user_notes(user.id, "ev1", "one")
    assert first.user_notes == "one"
    second = await meeting_notes.append_user_notes(user.id, "ev1", "two")
    assert second.user_notes == "one\n\ntwo"


async def test_clear_fathom_keeps_user_notes(user):
    await meeting_notes.upsert(user.id, "keep", title="K", recording_id="r1", fathom_url="u",
                               summary="s", action_items=[{"t": 1}], user_notes="mine")
    await meeting_notes.upsert(user.id, "drop", title="D", recording_id="r2", summary="s")
    await meeting_notes.upsert(user.id, "cal", title="C", asked_at=timeutil.now())
    assert await meeting_notes.clear_fathom(user.id) == 2
    assert (await meeting_notes.get(user.id, "cal")).asked_at is not None
    kept = await meeting_notes.get(user.id, "keep")
    assert kept.user_notes == "mine" and kept.recording_id is None and kept.summary == ""
    assert kept.fathom_url == "" and kept.action_items == []
    assert await meeting_notes.get(user.id, "drop") is None


async def test_window_and_asked(user):
    t = timeutil.now()
    await meeting_notes.upsert(user.id, "in", title="in", starts_at=t, ends_at=t + timedelta(hours=1))
    await meeting_notes.upsert(user.id, "out", title="out", starts_at=t + timedelta(days=3))
    rows = await meeting_notes.in_window(user.id, t - timedelta(hours=1), t + timedelta(days=1))
    assert [r.event_key for r in rows] == ["in"]
    await meeting_notes.mark_asked(user.id, "in", t)
    assert (await meeting_notes.get(user.id, "in")).asked_at is not None
