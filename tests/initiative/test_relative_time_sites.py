"""A3: every place that shows a due time renders it through timefmt.relative_due with the clock read at
render time, and times extracted from a source are anchored to that source's own time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain.decisions import ComposedMessage
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.domain.loops import Loop, LoopKind, LoopUpsert
from mavis.domain.memory import Extraction, LoopDraft
from mavis.initiative import routines as routines_mod
from mavis.initiative.filters import summarize_event
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.routines import MORNING_ROUTINE
from mavis.initiative.wiring import build_initiative
from mavis.memory.recall import render_loop
from mavis.policy.pings import PingPolicy

IST = "Asia/Kolkata"


def ist(d: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 10, d, h, m, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def _loop(title: str, due: datetime | None, kind=LoopKind.COMMITMENT) -> Loop:
    return Loop(id=7, user_id=1, kind=kind, title=title, due_at=due, trust=Trust.USER)


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


@pytest.fixture(autouse=True)
def _no_sources():
    routines_mod.clear_brief_sources()
    yield
    routines_mod.clear_brief_sources()


# --- chat context (recall) ---------------------------------------------------------------------------


@pytest.mark.parametrize("now,due,expected", [
    (ist(3, 18, 48), ist(3, 13, 0), "overdue by 5h 48m (was due 13:00)"),
    (ist(3, 18, 48), ist(3, 19, 0), "due in 12 min (19:00)"),
    (ist(3, 8, 0), ist(4, 9, 30), "due tomorrow 09:30"),
])
def test_recall_line_is_relative_to_the_render_clock(now, due, expected):
    line = render_loop(_loop("Renew the lease", due), IST, now)
    assert line == f"Renew the lease (commitment, {expected})"


def test_recall_line_for_an_undated_loop_has_no_time():
    line = render_loop(_loop("Learn Spanish", None, LoopKind.GOAL), IST, ist(3, 8, 0))
    assert line == "Learn Spanish (goal)"


async def test_recall_uses_the_current_clock(clock):
    from mavis.memory.recall import recall

    class Loops:
        async def active(self, user_id, entities=None, due_within=None):
            return [_loop("Call the bank", ist(3, 9, 0))]

    class Empty:
        async def get(self, uid):
            class Spotter:
                def spot(self, text):
                    return []
            return Spotter()

        async def neighborhood(self, *a, **k):
            return []

        async def search_with_kind(self, *a, **k):
            return []

        async def search_hits(self, *a, **k):
            return []

    clock.set(ist(3, 9, 30))
    ctx = await recall(1, "x", profile="", tz=IST, spotters=Empty(), graph=Empty(), vector=Empty(),
                       loops=Loops())
    assert ctx.loops == ["Call the bank (commitment, overdue by 30 min (was due 09:00))"]


# --- reasoner context ------------------------------------------------------------------------------


async def test_reasoner_loop_lines_are_relative(user, clock, fake_memory, monkeypatch):
    from mavis.initiative.filters import FilterResult
    from mavis.llm import models as llm

    seen: dict = {}

    async def fake(schema, system, user_msg, **kw):
        seen["user"] = user_msg
        from mavis.domain.decisions import InitiativeDecision
        return InitiativeDecision(ignore_reason="t")

    monkeypatch.setattr(llm, "structured", fake)
    clock.set(ist(3, 18, 48))
    loops = [_loop("Security alert", ist(3, 13, 0)), _loop("Workshop", ist(3, 19, 0)),
             _loop("Taxes", ist(9, 10, 0)), _loop("Read a book", None, LoopKind.GOAL)]
    ev = Event(id="wakeup:1", user_id=user.id, type=EventType.WAKEUP, occurred_at=clock.t, source="timer",
               payload={"kind": "agent", "reason": "r"})
    await Reasoner(fake_memory, PingPolicy()).decide(
        user, ev, FilterResult(drop=False, relevance=0.5, summary="s", matched_loops=loops))
    text = seen["user"]
    assert "'Security alert' overdue by 5h 48m (was due 13:00)" in text
    assert "'Workshop' due in 12 min (19:00)" in text
    assert "'Taxes' due Fri 9 Oct 10:00" in text
    assert "'Read a book' no due date" in text


@pytest.mark.parametrize("now,due,expected", [
    (ist(3, 18, 48), ist(3, 13, 0), "overdue by 5h 48m"),
    (ist(3, 12, 0), ist(3, 13, 0), "due in 1h"),
    (ist(3, 12, 0), None, "no due date"),
])
def test_loop_event_summary_is_relative(now, due, expected):
    payload = _loop("Pay rent", due).model_dump(mode="json")
    ev = Event(id="loop:7:created", user_id=1, type=EventType.LOOP_CREATED, occurred_at=now, source="agent",
               payload=payload)
    assert expected in summarize_event(ev, tz=IST, now=now)


# --- morning brief ---------------------------------------------------------------------------------


async def test_brief_items_are_relative_and_overdue_ones_say_so(user, clock, recording_bus, fake_memory,
                                                               monkeypatch):
    clock.set(ist(3, 9, 0))  # brief runs late at 09:00
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    for title, due in (("Dentist", ist(3, 8, 0)), ("Standup", ist(3, 9, 20)), ("Dinner", ist(3, 20, 0))):
        await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title=title, due_at=due,
                                                    trust=Trust.USER))
    seen: dict = {}

    async def spy(self, user_, intent, urgency, context="", **kw):
        seen["intent"] = intent
        return ComposedMessage(send=False, messages=[])

    from mavis.initiative.composer import Composer
    monkeypatch.setattr(Composer, "compose", spy)
    await init.routines.run(user, {"routine": MORNING_ROUTINE, "loop_id": None})
    assert "Dentist, overdue by 1h (was due 08:00)" in seen["intent"]
    assert "Standup, due in 20 min (09:20)" in seen["intent"]
    assert "Dinner, due today 20:00 (in 11h)" in seen["intent"]


# --- time anchoring: extracted times resolve against the source's own time ---------------------------


async def test_learn_anchors_extraction_to_the_source_time(user, memory, fake_llm, clock):
    clock.set(ist(3, 13, 35))  # LEARN runs an hour after the turn and a day after the email
    fake_llm.push_structured(Extraction())
    written = ist(2, 15, 42)
    await memory.learn(user.id, "Registration confirmed: workshop at 7 PM", "gmail:x", Trust.UNTRUSTED,
                       conversation=False, anchor_at=written)
    system = fake_llm.structured_calls[-1]["system"]
    assert "Friday 2026-10-02 15:42" in system
    assert "Saturday 2026-10-03" not in system


async def test_user_relative_day_is_resolved_against_when_they_said_it(user, memory, fake_llm, clock):
    """'tomorrow' typed at 23:30 on the 2nd and learned at 01:30 on the 3rd still means the 3rd."""
    clock.set(ist(3, 9, 0))
    fake_llm.push_structured(Extraction(loops=[LoopDraft(kind="commitment", title="Send the invoice",
                                                         due_at=datetime(2026, 10, 9, 10, 0))]))
    seen = []

    async def hook(uid, extraction, prov):
        seen.append(extraction)

    memory.on_extraction.append(hook)
    await memory.learn(user.id, "User: I'll send the invoice tomorrow at 10", "tg:update:5", Trust.USER,
                       anchor_at=ist(2, 20, 0))
    [ex] = seen
    assert ex.loops[0].due_at.date().isoformat() == "2026-10-03"


async def test_turn_learn_job_carries_the_turn_time(user, rec_bus, clock):
    from mavis.agents.turn_support import enqueue_learn

    said_at = ist(3, 12, 34)
    clock.set(said_at + timedelta(minutes=1))
    ev = Event(id="tg:update:9", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=said_at,
               source="telegram", payload={"text": "hi"}, trust=Trust.USER)
    await enqueue_learn(user.id, ev, "workshop at 7", None)
    [job] = [j for j in rec_bus.jobs if j.kind is JobKind.LEARN]
    assert datetime.fromisoformat(job.payload["anchor_at"]) == said_at


async def test_learn_job_hands_its_anchor_to_memory(user, monkeypatch, clock):
    from mavis.memory import jobs

    got = {}

    class Mem:
        async def learn(self, user_id, text, source_ref="", trust=Trust.USER, conversation=True,
                        anchor_at=None):
            got["anchor_at"] = anchor_at

    async def noop(uid):
        return False

    monkeypatch.setattr(jobs, "get_memory", lambda: Mem())
    monkeypatch.setattr(jobs, "maybe_summarize", noop)
    anchor = ist(2, 15, 42)
    await jobs.handle_learn(Job(id="learn:z", user_id=user.id, kind=JobKind.LEARN,
                                payload={"text": "t", "source_ref": "z", "anchor_at": anchor.isoformat()}))
    assert got["anchor_at"] == anchor
