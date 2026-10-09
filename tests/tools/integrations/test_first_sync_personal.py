# ruff: noqa: E501
"""First sync feeds the personal layer: calendar occurrences become routines, and a rebuild is asked for."""

from __future__ import annotations

from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability
from mavis.domain.wakeups import WakeupKind
from mavis.memory import personal, personal_layer
from mavis.store.repo import users
from mavis.store.repo import wakeups as wakeups_repo
from mavis.tools.integrations.first_sync import FirstSync
from tests.tools.integrations.fakes import NOW
from tests.tools.integrations.test_first_sync import FakeLoops, FakeMemory, tz_of


def weekly(summary, hour_utc, days):
    return [{"id": f"{summary}-{d}", "summary": summary, "start": {"dateTime": f"2026-09-{d:02d}T{hour_utc:02d}:00:00+00:00"},
             "attendees": [{"email": "lena@northwind.io"}]} for d in days]


async def test_calendar_first_sync_records_occurrences_and_asks_for_a_layer(db, provider, fake_bus):
    user, _ = await users.get_or_create_by_chat(901, "Dev")
    provider.results["calendar.list"] = ToolResult(ok=True, data={"items": [
        *weekly("Platform standup", 4, (14, 21, 28)),
        {"id": "one", "summary": "Dentist", "start": {"dateTime": "2026-09-30T10:00:00+00:00"}}]})
    sync = FirstSync(provider=provider, memory=FakeMemory(), loops=FakeLoops(), bus=fake_bus, tz_of=tz_of, clock=lambda: NOW,
                     meetings=personal_layer.record_meetings, after_sync=personal_layer.schedule)
    await sync.run(user.id, Capability.CALENDAR)
    (routine,) = await personal.meeting_evidence(user.id)
    assert "Platform standup" in routine.text and "weekly" in routine.text and "3 occurrences" in routine.text
    pending = await wakeups_repo.list_pending(user.id, WakeupKind.SYSTEM_PERSONAL_LAYER)
    assert [w.payload.get("mode") for w in pending] == ["soon"]
    await sync.run(user.id, Capability.GMAIL)  # another connector finishing does not queue a second rebuild
    assert len(await wakeups_repo.list_pending(user.id, WakeupKind.SYSTEM_PERSONAL_LAYER)) == 1


async def test_a_failing_layer_hook_never_fails_the_sync(provider, fake_bus):
    async def boom(user_id):
        raise RuntimeError("down")

    provider.results["calendar.list"] = ToolResult(ok=True, data={"items": []})
    sync = FirstSync(provider=provider, memory=FakeMemory(), loops=FakeLoops(), bus=fake_bus, tz_of=tz_of, clock=lambda: NOW, after_sync=boom)
    assert await sync.run(1, Capability.CALENDAR) == []
    assert fake_bus.events


async def test_paused_calendar_records_nothing(db, provider, fake_bus):
    from mavis.memory import controls

    user, _ = await users.get_or_create_by_chat(902, "Tara")
    await controls.set_paused(user.id, "calendar", True)
    provider.results["calendar.list"] = ToolResult(ok=True, data={"items": weekly("Lecture", 4, (14, 21))})
    sync = FirstSync(provider=provider, memory=FakeMemory(), loops=FakeLoops(), bus=fake_bus, tz_of=tz_of, clock=lambda: NOW,
                     meetings=personal_layer.record_meetings)
    await sync.run(user.id, Capability.CALENDAR)
    assert await personal.meeting_evidence(user.id) == []
