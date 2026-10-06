"""T2: every writer of a stored loop title goes through the one relative-date normaliser, anchored to when
the source text was written: LEARN extraction (the turn's time, not the job's), the reasoner's track (its
run), the track_loop tool (the call), and any upsert through LoopService."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from mavis.domain.decisions import InitiativeDecision
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopUpsert
from mavis.domain.memory import ExtractedEvent, Extraction, LoopDraft
from mavis.loops.service import LoopService, loops_from_extraction
from mavis.store.repo import users

IST, NYC, LON, AKL = "Asia/Kolkata", "America/New_York", "Europe/London", "Pacific/Auckland"


def local(tz: str, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(2026, 10, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


async def _in_zone(user, tz: str):
    await users.update(user.id, timezone=tz)
    return await users.get(user.id)


@pytest.mark.parametrize("tz", [IST, NYC, LON, AKL])
async def test_upsert_resolves_relative_title_at_write_time(user, recording_bus, clock, tz):
    await _in_zone(user, tz)
    clock.set(local(tz, 3, 18, 0))  # Saturday
    loop = await LoopService(recording_bus).upsert(
        user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dinner with Priya tonight", trust=Trust.USER))
    assert loop.title == "Dinner with Priya Sat 3 Oct evening"


async def test_upsert_resolves_against_an_explicit_anchor(user, recording_bus, clock):
    clock.set(local(IST, 6, 10, 0))
    loop = await LoopService(recording_bus).upsert(
        user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Block 2 to 3 PM tomorrow", trust=Trust.USER),
        anchor_at=local(IST, 3, 18, 0))
    assert loop.title == "Block 2 to 3 PM Sun 4 Oct"


async def test_update_by_id_normalises_a_new_title(user, recording_bus, clock):
    clock.set(local(IST, 3, 18, 0))
    svc = LoopService(recording_bus)
    loop = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Pay rent", trust=Trust.USER))
    updated = await svc.upsert(user.id, LoopUpsert(id=loop.id, title="Pay rent by Monday"))
    assert updated.title == "Pay rent by Mon 5 Oct"


async def test_titles_without_relative_words_are_stored_as_written(user, recording_bus, clock):
    clock.set(local(IST, 3, 18, 0))
    loop = await LoopService(recording_bus).upsert(
        user.id, LoopUpsert(kind=LoopKind.GOAL, title="Learn Spanish every Monday", trust=Trust.USER))
    assert loop.title == "Learn Spanish every Monday"


async def test_learn_resolves_titles_at_the_turn_time_not_the_job_time(memory, user, recording_bus, clock,
                                                                      fake_llm):
    await _in_zone(user, NYC)
    svc = LoopService(recording_bus)

    async def hook(uid, extraction, prov):
        await loops_from_extraction(svc, uid, extraction, prov)

    memory.on_extraction.append(hook)
    clock.set(local(NYC, 5, 9, 0))  # the LEARN job runs on Monday (retries, backlog)...
    fake_llm.push_structured(Extraction(
        loops=[LoopDraft(kind="COMMITMENT", title="Block around 2 pm tomorrow")],
        events=[ExtractedEvent(title="Lunch with Sam this Friday", starts_at=local(NYC, 9, 13, 0),
                               importance=4)],
    ))
    # ...for a message written on Saturday
    await memory.learn(user.id, "User: block around 2 pm tomorrow, and lunch with Sam this Friday",
                       "tg:update:9", Trust.USER, anchor_at=local(NYC, 3, 18, 0))
    titles = sorted(lp.title for lp in await svc.active(user.id))
    assert titles == ["Block around 2 pm Sun 4 Oct", "Lunch with Sam Fri 9 Oct"]


async def test_reasoner_track_titles_are_resolved(user, clock, recording_bus, fake_memory):
    from mavis.initiative.composer import Composer
    from mavis.initiative.executor import InitiativeExecutor
    from mavis.initiative.quiet import QuietTracker
    from mavis.policy.pings import PingPolicy
    from mavis.timers.service import WakeupService

    user = await _in_zone(user, AKL)
    clock.set(local(AKL, 7, 9, 0))  # Wednesday
    wakeups = WakeupService()
    loops = LoopService(recording_bus)
    executor = InitiativeExecutor(recording_bus, loops, wakeups, PingPolicy(), Composer(fake_memory),
                                  QuietTracker(wakeups))
    ev = Event(id="wakeup:3", user_id=user.id, type=EventType.WAKEUP, occurred_at=clock.t, source="timer",
               payload={"kind": "agent", "reason": "r"}, trust=Trust.SYSTEM)
    await executor.apply(user, InitiativeDecision(
        track=[LoopUpsert(kind=LoopKind.WAITING_ON, title="Recruiter reply expected next week")]), ev)
    [loop] = await loops.active(user.id)
    assert loop.title == "Recruiter reply expected week of 12 Oct"


async def test_track_loop_tool_title_is_resolved_at_call_time(user, recording_bus, clock, monkeypatch):
    from mavis import bus as bus_mod
    from mavis.tools import assistant

    monkeypatch.setattr(bus_mod, "get_bus", lambda: recording_bus)
    monkeypatch.setattr(assistant.bus, "get_bus", lambda: recording_bus)
    await _in_zone(user, LON)
    clock.set(local(LON, 3, 18, 0))
    out = await assistant.track_loop(user.id, assistant.TrackLoopArgs(
        kind=LoopKind.COMMITMENT, title="Send the deck in 2 days"))
    assert "Send the deck on Mon 5 Oct" in out


def test_prompts_ask_for_absolute_dates_in_titles():
    from mavis.initiative.reasoner import REASONER_SYSTEM
    from mavis.memory.extractor import SYSTEM_PROMPT
    from mavis.tools.assistant import TrackLoopArgs

    assert "absolute dates" in SYSTEM_PROMPT and "titles" in SYSTEM_PROMPT.lower()
    assert "absolute dates" in REASONER_SYSTEM and "`track` titles" in REASONER_SYSTEM
    desc = TrackLoopArgs.model_json_schema()["properties"]["title"].get("description", "")
    assert "absolute date" in desc
